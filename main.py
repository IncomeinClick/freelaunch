"""freelaunch — organic launch planning + tracking for Pond's projects."""
import base64
import json
import os
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

BASE = Path("/opt/freelaunch")
DB_PATH = BASE / "freelaunch.db"

AUTH_USER = os.environ.get("FREELAUNCH_USER", "")
AUTH_PASS = os.environ.get("FREELAUNCH_PASS", "")
SESSION_SECRET = os.environ.get("FREELAUNCH_SECRET", "change-me")
SESSION_MAX_AGE = 60 * 60 * 24 * 90  # 90 days

app = FastAPI(title="freelaunch")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# --- Auth middleware --------------------------------------------------------

LOGIN_HTML = """<!doctype html>
<html lang=\"en\"><head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>freelaunch — login</title>
<link rel=\"stylesheet\" href=\"/static/style.css\">
<style>
.login-wrap{max-width:380px;margin:80px auto;padding:32px;background:#1a1d24;border-radius:12px;border:1px solid #2a2f3a;}
.login-wrap h1{margin:0 0 6px;font-size:28px;}
.login-wrap h1 span{color:#22c55e;}
.login-wrap .sub{color:#888;font-size:13px;margin-bottom:24px;}
.login-wrap label{display:block;font-size:12px;color:#aaa;margin:14px 0 6px;}
.login-wrap input{width:100%;padding:10px 12px;background:#0f1217;border:1px solid #2a2f3a;border-radius:6px;color:#eee;font-size:14px;box-sizing:border-box;}
.login-wrap input:focus{outline:none;border-color:#22c55e;}
.login-wrap button{margin-top:20px;width:100%;padding:11px;background:#22c55e;color:#000;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;}
.login-wrap button:hover{background:#16a34a;}
.login-wrap .err{margin-top:14px;padding:10px;background:#3a1a1a;border:1px solid #6b2424;border-radius:6px;color:#f87171;font-size:13px;}
</style>
</head><body>
<div class=\"login-wrap\">
  <h1>free<span>launch</span></h1>
  <div class=\"sub\">Sign in to continue</div>
  __ERR__
  <form method=\"post\" action=\"/login\">
    <label>Email</label>
    <input type=\"text\" name=\"username\" autocomplete=\"username\" required autofocus>
    <label>Password</label>
    <input type=\"password\" name=\"password\" autocomplete=\"current-password\" required>
    <button type=\"submit\">Sign in</button>
  </form>
</div>
</body></html>"""


def render_login(error: str = "") -> str:
    err_html = f'<div class="err">{error}</div>' if error else ""
    return LOGIN_HTML.replace("__ERR__", err_html)


def check_basic(auth_header: str) -> bool:
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8", "ignore")
        u, _, p = decoded.partition(":")
        return (
            secrets.compare_digest(u, AUTH_USER)
            and secrets.compare_digest(p, AUTH_PASS)
        )
    except Exception:
        return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Public paths
    if path in ("/login", "/healthz") or path.startswith("/static/"):
        return await call_next(request)

    # Logged-in via session
    has_session = bool(request.session.get("user"))

    # API routes accept either session OR basic auth (for cron/scripts)
    if path.startswith("/api/"):
        if has_session or check_basic(request.headers.get("Authorization", "")):
            return await call_next(request)
        return JSONResponse(
            {"detail": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="freelaunch"'},
        )

    # UI routes require session — redirect to /login
    if has_session:
        return await call_next(request)
    return RedirectResponse("/login", status_code=303)


app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=True,
)


@app.get("/login")
def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(render_login())


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if secrets.compare_digest(username, AUTH_USER) and secrets.compare_digest(password, AUTH_PASS):
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(render_login("Invalid email or password"), status_code=401)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- DB ---------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    color TEXT DEFAULT '#22c55e',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    scheduled_date TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_project_date ON tasks(project_id, scheduled_date);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    metric_date TEXT NOT NULL,
    visitors INTEGER DEFAULT 0,
    pageviews INTEGER DEFAULT 0,
    trial_signups INTEGER DEFAULT 0,
    paid_signups INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, metric_date)
);
CREATE INDEX IF NOT EXISTS idx_metrics_project_date ON metrics(project_id, metric_date);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        existing = conn.execute("SELECT slug FROM projects").fetchall()
        slugs = {r["slug"] for r in existing}
        seed = [
            ("newton-th", "Newton TH", "Newton Thai: organic launch", "#22c55e"),
            ("newton-en", "Newton EN", "Newton English — SaaS launch from zero", "#3b82f6"),
        ]
        for slug, name, desc, color in seed:
            if slug not in slugs:
                conn.execute(
                    "INSERT INTO projects (slug, name, description, color) VALUES (?, ?, ?, ?)",
                    (slug, name, desc, color),
                )


# --- Models -----------------------------------------------------------------

class TaskIn(BaseModel):
    title: str
    description: Optional[str] = ""
    channel: str
    scheduled_date: str
    status: str = "todo"


class TaskPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    channel: Optional[str] = None
    scheduled_date: Optional[str] = None
    status: Optional[str] = None


# --- Routes -----------------------------------------------------------------

@app.get("/")
def root():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/p/{slug}")
def project_page(slug: str):
    return FileResponse(BASE / "static" / "project.html")


@app.get("/api/projects")
def list_projects():
    today = date.today().isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
              (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.status='todo' AND t.scheduled_date <= ?) AS open_today,
              (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.status='done') AS done_total,
              (SELECT trial_signups FROM metrics m WHERE m.project_id=p.id AND m.metric_date=?) AS trials_today,
              (SELECT visitors FROM metrics m WHERE m.project_id=p.id AND m.metric_date=?) AS visitors_today
            FROM projects p ORDER BY p.id
            """,
            (today, today, today),
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/projects/{slug}")
def get_project(slug: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "project not found")
        return dict(row)


@app.get("/api/projects/{slug}/tasks")
def list_tasks(slug: str, start: Optional[str] = None, end: Optional[str] = None):
    with db() as conn:
        proj = conn.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()
        if not proj:
            raise HTTPException(404, "project not found")
        q = "SELECT * FROM tasks WHERE project_id=?"
        params = [proj["id"]]
        if start:
            q += " AND scheduled_date >= ?"
            params.append(start)
        if end:
            q += " AND scheduled_date <= ?"
            params.append(end)
        q += " ORDER BY scheduled_date, id"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/projects/{slug}/tasks")
def create_task(slug: str, task: TaskIn):
    with db() as conn:
        proj = conn.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()
        if not proj:
            raise HTTPException(404, "project not found")
        cur = conn.execute(
            "INSERT INTO tasks (project_id, scheduled_date, title, description, channel, status) VALUES (?, ?, ?, ?, ?, ?)",
            (proj["id"], task.scheduled_date, task.title, task.description or "", task.channel, task.status),
        )
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, patch: TaskPatch):
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "task not found")
        updates = {k: v for k, v in patch.dict().items() if v is not None}
        if not updates:
            return dict(row)
        if "status" in updates and updates["status"] == "done" and row["status"] != "done":
            updates["completed_at"] = datetime.utcnow().isoformat()
        if "status" in updates and updates["status"] != "done":
            updates["completed_at"] = None
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*updates.values(), task_id))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row)


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    with db() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"ok": True}


@app.get("/api/projects/{slug}/metrics")
def list_metrics(slug: str, days: int = 14):
    with db() as conn:
        proj = conn.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()
        if not proj:
            raise HTTPException(404, "project not found")
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        rows = conn.execute(
            "SELECT * FROM metrics WHERE project_id=? AND metric_date>=? ORDER BY metric_date",
            (proj["id"], cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/healthz")
def healthz():
    return {"ok": True}


init_db()
