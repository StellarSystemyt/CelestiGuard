# dashboard.py
from __future__ import annotations

import os
import json
import secrets
import time
import asyncio
import threading
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, List

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from urllib.parse import urlencode

APP_TITLE = "CelestiGuard Dashboard"
VERSION = os.getenv("CELESTIGUARD_VERSION", "dev")

app = FastAPI(title=APP_TITLE)

# Basic logger
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("celestiguard")

# --- Templates & Static (absolute paths so systemd/AWS can't break them) ---
BASE_DIR = Path(__file__).resolve().parent
templates_dir = BASE_DIR / "templates"
static_dir = BASE_DIR / "static"

if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
elif templates_dir.is_dir():
    # Legacy assets might live in templates/
    app.mount("/static", StaticFiles(directory=str(templates_dir)), name="static")

templates = Jinja2Templates(directory=str(templates_dir)) if templates_dir.is_dir() else None

# --- Persistent dedupe for OAuth (shared across workers) ---
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
OAUTH_DB_PATH = DATA_DIR / "oauth_cache.db"

def _oauth_db():
    conn = sqlite3.connect(str(OAUTH_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS used_states (
            state TEXT PRIMARY KEY,
            ts INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS used_codes (
            code TEXT PRIMARY KEY,
            ts INTEGER NOT NULL
        )
    """)
    return conn

def mark_state_used_once(state: str) -> bool:
    """True if first time we've seen this state; False if already used."""
    with _oauth_db() as c:
        try:
            c.execute("INSERT INTO used_states(state, ts) VALUES(?, strftime('%s','now'))", (state,))
            return True
        except sqlite3.IntegrityError:
            return False

def mark_code_used_once(code: str) -> bool:
    """True if first time we've seen this code; False if already used."""
    with _oauth_db() as c:
        try:
            c.execute("INSERT INTO used_codes(code, ts) VALUES(?, strftime('%s','now'))", (code,))
            return True
        except sqlite3.IntegrityError:
            return False

# --- Small helpers ---
def _find_changelog_path() -> Path | None:
    candidates = [
        DATA_DIR / "changelog.json",
        BASE_DIR / "changelog.json",
        templates_dir / "changelog.json",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None

def _no_store_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }

# --- Health & Version ---
@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "version": VERSION})

@app.get("/api/version")
async def api_version():
    return JSONResponse({"version": VERSION})

# --- API endpoint for changelog ---
@app.get("/api/changelog")
async def api_changelog():
    """
    Always return a JSON LIST (possibly empty) and disable caching so the page
    never gets stuck on stale responses.
    """
    p = _find_changelog_path()
    items: List[Any] = []
    if p:
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                items = [data]
            elif isinstance(data, list):
                items = data
            else:
                items = []
        except Exception:
            items = []
    return JSONResponse(items, headers=_no_store_headers())

# --- Webpage route ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """
    Renders index.html if templates/ exists, otherwise serves a tiny inline page
    that fetches /api/changelog and shows it.
    """
    if templates:
        resp = templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "title": APP_TITLE,
                "version": VERSION,
            },
        )
        # Debug header to quickly see if the browser sent a session
        resp.headers["X-Debug-Session"] = "present" if request.cookies.get("session") else "absent"
        return resp
from fastapi import Response

@app.head("/")
def home_head():
    # Return 200 for HEAD so Nginx/probes don't see 405
    return Response(status_code=200)

    # Fallback minimal HTML if Jinja templates aren't available
    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width,initial-scale=1" />
        <title>{APP_TITLE}</title>
        <style>
          body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; }}
          .muted {{ color: #6b7280; }}
          .card {{ border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; margin: 12px 0; }}
        </style>
      </head>
      <body>
        <h1>{APP_TITLE} <span class="muted">v{VERSION}</span></h1>
        <h2>Changelog</h2>
        <div id="cl">Loading…</div>

        <script>
          (async function() {{
            const el = document.getElementById('cl');
            try {{
              const res = await fetch('/api/changelog', {{ cache: 'no-store', headers: {{ 'Cache-Control': 'no-store' }} }});
              if (!res.ok) throw new Error('HTTP ' + res.status);
              const items = await res.json();
              if (!Array.isArray(items) || !items.length) {{
                el.textContent = 'No changelog entries yet.';
                return;
              }}
              el.innerHTML = items.map(entry => `
                <div class="card">
                  <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
                    <strong>${{entry.version || 'unversioned'}}</strong>
                    <span class="muted">${{entry.date || ''}}</span>
                  </div>
                  <ul style="margin:10px 0 0 18px">
                    ${{(entry.changes || []).map(c => `<li>${{c}}</li>`).join('')}}
                  </ul>
                </div>
              `).join('');
            }} catch (e) {{
              el.textContent = 'Failed to load changelog.';
            }}
          }})();
        </script>
      </body>
    </html>
    """
    return HTMLResponse(html)

# --- Small niceties to reduce log noise ---
@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

@app.get("/robots.txt", response_class=HTMLResponse)
def robots():
    return HTMLResponse("User-agent: *\nDisallow:\n", media_type="text/plain")

# --- OAuth (Discord) ---
DISCORD_AUTH   = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN  = "https://discord.com/api/oauth2/token"
CLIENT_ID      = os.getenv("OAUTH_CLIENT_ID", "")
CLIENT_SECRET  = os.getenv("OAUTH_CLIENT_SECRET", "")  # used in token exchange
REDIRECT_URI   = os.getenv("OAUTH_REDIRECT_URI", "https://celestiguard.xyz/auth/callback")
SCOPES         = ["identify", "guilds"]

# still keep light in-memory checks (good fast path), but persistence is the fence
_used_states_mem: dict[str, float] = {}
_used_codes_mem: dict[str, float] = {}
_code_lock = threading.Lock()

# Optional: cookie domain override (leave empty to omit)
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", "").strip() or None

@app.get("/auth/login")
def auth_login(request: Request):
    # HARD STOP: if a session already exists, don't start a new OAuth flow
    if request.cookies.get("session"):
        resp = RedirectResponse("/", status_code=303)
        resp.headers["X-Debug-Stage"] = "auth/login-session-exists"
        return resp

    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        "prompt": "none",  # or "consent"
    }
    url = f"{DISCORD_AUTH}?{urlencode(params)}"
    log.info("auth_login -> redirecting to Discord | state=%s", state)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        "oauth_state", state,
        max_age=300, secure=True, httponly=True, samesite="lax", path="/",
        domain=COOKIE_DOMAIN
    )
    resp.headers["X-Debug-Stage"] = "auth/login"
    resp.headers["X-Debug-State"] = state
    return resp

async def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
    """Exchange authorization code for tokens. Retries a few times on rate limit."""
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    attempts = 0
    while True:
        attempts += 1
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                DISCORD_TOKEN,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code == 200:
            return resp.json()
        # Basic backoff on rate limit (Discord sometimes replies 429 or 400 w/ rate-limit phrasing)
        if resp.status_code in (400, 429):
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 1.0
            else:
                wait = {1: 0.5, 2: 1.0, 3: 2.0}.get(attempts, 0)
            if attempts <= 3 and wait > 0:
                await asyncio.sleep(min(wait, 5.0))
                continue
        # Bubble up error details for troubleshooting
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str | None = None, state: str | None = None):
    if not code or not state:
        log.info("auth_callback -> missing code/state | code=%s state=%s", code, state)
        raise HTTPException(400, "Missing code")

    cookie_state = request.cookies.get("oauth_state")
    if not cookie_state or cookie_state != state:
        log.info("auth_callback -> invalid state | cookie=%s query=%s", cookie_state, state)
        raise HTTPException(400, "Invalid state")

    # Fast-path memory dedupe (helps when single worker)
    if state in _used_states_mem:
        log.info("auth_callback -> state already used (mem) | state=%s", state)
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie("oauth_state", path="/", domain=COOKIE_DOMAIN)
        resp.headers["X-Debug-Stage"] = "auth/callback-already-used-mem"
        return resp

    with _code_lock:
        if code in _used_codes_mem:
            log.info("auth_callback -> code already used (mem) | code=%s", code[:8])
            resp = RedirectResponse("/", status_code=303)
            resp.delete_cookie("oauth_state", path="/", domain=COOKIE_DOMAIN)
            resp.headers["X-Debug-Stage"] = "auth/callback-code-reused-mem"
            return resp

    # Durable, cross-worker idempotency (SQLite)
    if not mark_state_used_once(state):
        log.info("auth_callback -> state already used (db) | state=%s", state)
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie("oauth_state", path="/", domain=COOKIE_DOMAIN)
        resp.headers["X-Debug-Stage"] = "auth/callback-already-used-db"
        return resp

    if not mark_code_used_once(code):
        log.info("auth_callback -> code already used (db) | code=%s", code[:8])
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie("oauth_state", path="/", domain=COOKIE_DOMAIN)
        resp.headers["X-Debug-Stage"] = "auth/callback-code-reused-db"
        return resp

    # Mark memory maps once we've passed the DB fence
    _used_states_mem[state] = time.time()
    _used_codes_mem[code] = time.time()

    log.info("auth_callback -> exchanging code once | code=%s state=%s", code[:8], state)
    try:
        token_payload = await exchange_code_for_token(code, REDIRECT_URI)
    except HTTPException as e:
        log.warning("auth_callback -> token exchange failed | status=%s", e.status_code)
        resp = JSONResponse(
            {"stage": "token", "status": e.status_code, "detail": "token_exchange_failed"},
            status_code=e.status_code,
            headers={"X-Debug-Stage": "auth/callback-exchange-failed"},
        )
        resp.delete_cookie("oauth_state", path="/", domain=COOKIE_DOMAIN)
        return resp

    # TODO: get user with token_payload["access_token"], then create your real session
    session_value = "sess_" + secrets.token_urlsafe(24)

    log.info("auth_callback -> success, setting session and redirecting home | session=%s...", session_value[:12])
    resp = RedirectResponse("/", status_code=303)  # 303 avoids retry loops
    resp.delete_cookie("oauth_state", path="/", domain=COOKIE_DOMAIN)
    resp.set_cookie(
        "session", session_value,
        max_age=60*60*24*7, secure=True, httponly=True, samesite="lax", path="/",
        domain=COOKIE_DOMAIN
    )
    resp.headers["X-Debug-Stage"] = "auth/callback-success"
    return resp

# --- Debug: see what cookies the browser is actually sending ---
@app.get("/debug/session")
def debug_session(request: Request):
    return JSONResponse({
        "time": datetime.utcnow().isoformat() + "Z",
        "has_session": bool(request.cookies.get("session")),
        "session_prefix": (request.cookies.get("session") or "")[:12],
        "has_oauth_state_cookie": bool(request.cookies.get("oauth_state")),
    }, headers={"X-Debug-Stage": "debug/session"})
