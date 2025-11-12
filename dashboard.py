# dashboard.py
from __future__ import annotations

import os
import json
import secrets
import time
from pathlib import Path
from typing import Any, List

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from urllib.parse import urlencode

APP_TITLE = "CelestiGuard Dashboard"
VERSION = os.getenv("CELESTIGUARD_VERSION", "dev")

app = FastAPI(title=APP_TITLE)

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


# --- Small helpers ---
def _find_changelog_path() -> Path | None:
    candidates = [
        BASE_DIR / "data" / "changelog.json",
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
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "title": APP_TITLE,
                "version": VERSION,
            },
        )

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
DISCORD_AUTH  = "https://discord.com/api/oauth2/authorize"
CLIENT_ID     = os.getenv("OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "")  # for token exchange later if/when you add it
REDIRECT_URI  = os.getenv("OAUTH_REDIRECT_URI", "https://celestiguard.xyz/auth/callback")
SCOPES        = ["identify", "guilds"]

# one-time state store to avoid loops (swap for Redis if you want)
_used_states: dict[str, float] = {}

@app.get("/auth/login")
def auth_login():
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        "prompt": "none",  # or "consent"
    }
    resp = RedirectResponse(f"{DISCORD_AUTH}?{urlencode(params)}", status_code=302)
    # bind state to client (5-minute TTL)
    resp.set_cookie("oauth_state", state, max_age=300, secure=True, httponly=True, samesite="lax", path="/")
    return resp

@app.get("/auth/callback")
def auth_callback(request: Request, code: str | None = None, state: str | None = None):
    if not code or not state:
        raise HTTPException(400, "Missing code")

    cookie_state = request.cookies.get("oauth_state")
    if not cookie_state or cookie_state != state:
        raise HTTPException(400, "Invalid state")

    # idempotent: ignore repeats so browsers/preloads don't loop
    if state in _used_states:
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie("oauth_state", path="/")
        return resp
    _used_states[state] = time.time()

    # TODO: exchange `code` for tokens (Discord token endpoint), then create your session
    # token = ...
    # user  = ...
    # session_value = ...

    resp = RedirectResponse("/", status_code=303)  # 303 avoids retry loops
    resp.delete_cookie("oauth_state", path="/")
    resp.set_cookie(
        "session", "your-session-token",
        max_age=60*60*24*7, secure=True, httponly=True, samesite="lax", path="/"
        # domain="celestiguard.xyz"  # optional; omit if unsure
    )
    return resp