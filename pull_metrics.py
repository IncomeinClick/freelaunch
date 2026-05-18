"""Daily metrics pull — Newton trial/paid signups (from DB) + traffic (from GA4 Data API).

GA4 filters bots automatically (JS-fired events only), so visitor numbers reflect real humans.

Run via cron 1x/hour (and ad-hoc to backfill).
"""
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

FREELAUNCH_DB = Path("/opt/freelaunch/freelaunch.db")
GA_TOKENS = Path("/root/.gcp/google-tokens.json")
GA_CLIENT = Path("/root/.gcp/oauth-client.json")

# project slug → config
# Two source types:
#   - source="newton_db": query Newton subscriptions DB for trial/paid signups
#   - source="ga_event":  pull conversion from a GA4 event (event_name = the conversion);
#                         stored in trial_signups column to reuse the schema
PROJECTS = {
    "newton-th": {
        "source": "newton_db",
        "newton_db": "/opt/newton/newton.db",
        "ga_property": "536898203",
        "hostname": "newton.incomeinclick.in.th",
    },
    "newton-en": {
        "source": "newton_db",
        "newton_db": "/opt/newton/newton-en.db",
        "ga_property": "536868580",
        "hostname": "newton.incomeinclick.com",
    },
    "whisperer": {
        "source": "ga_event",
        "event_name": "first_chat",
        "ga_property": "536162879",
        "hostname": "whisperer.chat",
    },
}


def refresh_ga_token() -> str:
    tokens = json.loads(GA_TOKENS.read_text())
    cfg = json.loads(GA_CLIENT.read_text())
    data = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": tokens["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]


def ga_run_report(property_id: str, body: dict, access_token: str) -> dict:
    req = urllib.request.Request(
        f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def collect_traffic(property_id: str, hostname: str, since: date, until: date, access_token: str) -> dict:
    """Return {date_str: {visitors, pageviews}} from GA4 (bots filtered)."""
    body = {
        "dateRanges": [{"startDate": since.isoformat(), "endDate": until.isoformat()}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "totalUsers"}, {"name": "screenPageViews"}],
        "dimensionFilter": {"filter": {"fieldName": "hostName", "stringFilter": {"value": hostname}}},
    }
    out: dict[str, dict[str, int]] = {}
    try:
        data = ga_run_report(property_id, body, access_token)
    except urllib.error.HTTPError as e:
        print(f"[error] GA API failed: {e.code} {e.read().decode()[:200]}")
        return out
    for row in data.get("rows", []):
        ga_date = row["dimensionValues"][0]["value"]  # YYYYMMDD
        iso = f"{ga_date[:4]}-{ga_date[4:6]}-{ga_date[6:8]}"
        out[iso] = {
            "visitors": int(row["metricValues"][0]["value"]),
            "pageviews": int(row["metricValues"][1]["value"]),
        }
    return out


def collect_signups(newton_db: str, since: date, until: date) -> dict:
    """Return {date_str: {trial_signups, paid_signups}}."""
    out: dict[str, dict[str, int]] = {}
    conn = sqlite3.connect(newton_db)
    try:
        trials = conn.execute(
            "SELECT date(created_at) AS d, COUNT(*) AS n FROM subscriptions WHERE is_trial=1 AND date(created_at) BETWEEN ? AND ? GROUP BY d",
            (since.isoformat(), until.isoformat()),
        ).fetchall()
        for d, n in trials:
            out.setdefault(d, {"trial_signups": 0, "paid_signups": 0})["trial_signups"] = n
        paid = conn.execute(
            "SELECT date(created_at) AS d, COUNT(*) AS n FROM subscriptions WHERE is_trial=0 AND date(created_at) BETWEEN ? AND ? GROUP BY d",
            (since.isoformat(), until.isoformat()),
        ).fetchall()
        for d, n in paid:
            out.setdefault(d, {"trial_signups": 0, "paid_signups": 0})["paid_signups"] = n
    finally:
        conn.close()
    return out


def collect_ga_event(property_id: str, event_name: str, since: date, until: date, access_token: str) -> dict:
    """Return {date_str: count} for a single GA4 event. Stored as trial_signups (the primary conversion)."""
    body = {
        "dateRanges": [{"startDate": since.isoformat(), "endDate": until.isoformat()}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "eventCount"}],
        "dimensionFilter": {"filter": {"fieldName": "eventName", "stringFilter": {"value": event_name}}},
    }
    out: dict[str, dict[str, int]] = {}
    try:
        data = ga_run_report(property_id, body, access_token)
    except urllib.error.HTTPError as e:
        print(f"[error] GA event ({event_name}) API failed: {e.code} {e.read().decode()[:200]}")
        return out
    for row in data.get("rows", []):
        ga_date = row["dimensionValues"][0]["value"]  # YYYYMMDD
        iso = f"{ga_date[:4]}-{ga_date[4:6]}-{ga_date[6:8]}"
        out[iso] = {"trial_signups": int(row["metricValues"][0]["value"]), "paid_signups": 0}
    return out


def upsert(project_slug: str, since: date, until: date, access_token: str) -> int:
    conf = PROJECTS[project_slug]
    traffic = collect_traffic(conf["ga_property"], conf["hostname"], since, until, access_token)
    if conf["source"] == "newton_db":
        signups = collect_signups(conf["newton_db"], since, until)
    elif conf["source"] == "ga_event":
        signups = collect_ga_event(conf["ga_property"], conf["event_name"], since, until, access_token)
    else:
        signups = {}

    conn = sqlite3.connect(FREELAUNCH_DB)
    try:
        proj = conn.execute("SELECT id FROM projects WHERE slug=?", (project_slug,)).fetchone()
        if not proj:
            print(f"[warn] project {project_slug} missing in freelaunch.db")
            return 0
        project_id = proj[0]

        days = {(since + timedelta(days=i)).isoformat() for i in range((until - since).days + 1)}
        days |= set(signups.keys())
        n = 0
        for d in sorted(days):
            t = traffic.get(d, {"visitors": 0, "pageviews": 0})
            s = signups.get(d, {"trial_signups": 0, "paid_signups": 0})
            conn.execute(
                """
                INSERT INTO metrics (project_id, metric_date, visitors, pageviews, trial_signups, paid_signups, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(project_id, metric_date) DO UPDATE SET
                  visitors=excluded.visitors,
                  pageviews=excluded.pageviews,
                  trial_signups=excluded.trial_signups,
                  paid_signups=excluded.paid_signups,
                  updated_at=excluded.updated_at
                """,
                (project_id, d, t["visitors"], t["pageviews"], s["trial_signups"], s["paid_signups"]),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    until = date.today()
    since = until - timedelta(days=days - 1)
    access_token = refresh_ga_token()
    for slug in PROJECTS:
        n = upsert(slug, since, until, access_token)
        print(f"[{slug}] upserted {n} days  ({since} → {until})")


if __name__ == "__main__":
    main()
