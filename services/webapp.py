from __future__ import annotations
import os, time, asyncio, json, secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.status import HTTP_303_SEE_OTHER
from starlette.middleware.sessions import SessionMiddleware
import httpx

from services.db import (
    get_conn, init,
    get_state, set_state,
    get_setting, set_setting,
    get_guild_config, set_guild_config,
)

__all__ = ["create_app", "set_bot", "set_brand_avatar"]

# ---------------- Globals ----------------
_bot = None
_brand_avatar_url: str | None = None
_START_TS = time.time()  # track uptime for /status

def set_bot(bot):  # called by bot.py
    global _bot
    _bot = bot

def set_brand_avatar(url: str | None):
    """Optional override for the dashboard logo/avatar."""
    global _brand_avatar_url
    _brand_avatar_url = url


# ---------------- App Factory ----------------
def create_app(version: str = "dev") -> FastAPI:
    """
    FastAPI app factory.
    Pass version from bot.py:  app = create_app(version=CELESTIGUARD_VERSION)
    """
    init()
    app = FastAPI(title="CelestiGuard Dashboard")

    # --- Session & Discord OAuth config ---
    SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")  # set a strong random value in .env
    OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "")
    OAUTH_CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "")
    OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "")
    DISCORD_API = "https://discord.com/api/v10"

    app.add_middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET,
        same_site="lax",
        https_only=True,  # set True if serving HTTPS directly here
    )

    # ---------- Auth (Discord OAuth) ----------
    def _is_logged_in(request: Request) -> bool:
        return "user" in request.session and "access_token" in request.session

    async def require_user(request: Request):
        if not _is_logged_in(request):
            # redirect to /auth/login (which jumps to Discord)
            raise HTTPException(status_code=302, detail="login", headers={"Location": "/auth/login"})
        return True

    async def _ensure_guilds_cached(request: Request):
        """Ensure session has the current user's guild IDs cached."""
        if not _is_logged_in(request):
            return
        if "guild_ids" in request.session:
            return
        token = request.session["access_token"]
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{DISCORD_API}/users/@me/guilds",
                                 headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            gids = [str(g.get("id")) for g in r.json() if g.get("id")]
            request.session["guild_ids"] = gids

    async def require_guild_member(request: Request, gid: int):
        await _ensure_guilds_cached(request)
        gids = set(request.session.get("guild_ids", []))
        if str(gid) not in gids:
            raise HTTPException(status_code=403, detail="You are not a member of this guild.")
        return True

    # Helper dependency to avoid lambda capturing issues
    async def _member_dep(request: Request, gid: int) -> bool:
        return await require_guild_member(request, gid)

    # ---- OAuth helpers ----
    def _authorize_url(request: Request) -> str:
        # Per Discord docs: space-delimited scopes; httpx encodes to %20
        state = secrets.token_urlsafe(24)
        request.session["oauth_state"] = state
        request.session["oauth_state_ts"] = int(time.time())
        params = {
            "client_id": OAUTH_CLIENT_ID,
            "response_type": "code",
            "scope": "identify guilds",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "state": state,            # CSRF protection
            # NOTE: deliberately no "prompt" param (fixes mobile/webview issues)
        }
        return f"https://discord.com/oauth2/authorize?{httpx.QueryParams(params)}"

        # ---- OAuth Routes ----
    def _env_problem() -> str | None:
        problems = []
        if not OAUTH_CLIENT_ID or "YOUR_" in OAUTH_CLIENT_ID:
            problems.append("OAUTH_CLIENT_ID is missing/placeholder.")
        if not OAUTH_CLIENT_SECRET or "YOUR_" in OAUTH_CLIENT_SECRET:
            problems.append("OAUTH_CLIENT_SECRET is missing/placeholder.")
        if not OAUTH_REDIRECT_URI or "YOUR_" in OAUTH_REDIRECT_URI:
            problems.append("OAUTH_REDIRECT_URI is missing/placeholder.")
        # Discord requires exact match (scheme, host, path) with your app settings
        # Example: https://dash.yourdomain.com/auth/callback  (no trailing slash if not in portal)
        return "\n".join(problems) if problems else None

    def _mini_help_page(title: str, body_html: str) -> HTMLResponse:
        html = f"""
        <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{title}</title>
        <style>
          body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; }}
          code, kbd {{ background:#1112; padding:2px 4px; border-radius:4px }}
          .card {{ border:1px solid #e5e7eb; border-radius:12px; padding:16px; }}
          .err {{ color:#b91c1c }}
        </style></head><body>
          <h1>{title}</h1>
          <div class="card">{body_html}</div>
        </body></html>
        """
        return HTMLResponse(html, status_code=400)

    @app.get("/auth/login")
    async def auth_login(request: Request):
        # Validate env first
        prob = _env_problem()
        if prob:
            return _mini_help_page(
                "Discord OAuth not configured",
                f"""
                <p class="err"><strong>Fix your environment variables:</strong></p>
                <pre>{prob}</pre>
                <p>Set (examples):</p>
                <pre>
export OAUTH_CLIENT_ID=123456789012345678
export OAUTH_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxx
export OAUTH_REDIRECT_URI=https://YOUR_DOMAIN/auth/callback
                </pre>
                <p>Make sure the redirect URL matches your Discord app <em>exactly</em>.</p>
                """
            )

        # Create CSRF state
        state = os.urandom(16).hex()
        request.session["oauth_state"] = state

        params = {
            "client_id": OAUTH_CLIENT_ID,
            "response_type": "code",
            "scope": "identify guilds",
            "redirect_uri": OAUTH_REDIRECT_URI,  # must exactly match Discord portal
            "state": state,
            "prompt": "none",
        }
        qp = httpx.QueryParams(params)
        url = f"https://discord.com/oauth2/authorize?{qp}"
        return RedirectResponse(url)

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        # Optional debug mode: /auth/callback?debug=1 to see raw errors
        debug = request.query_params.get("debug") == "1"
        code = request.query_params.get("code")
        error = request.query_params.get("error")
        state = request.query_params.get("state")

        if error:
            detail = {"stage": "authorize", "error": error}
            return JSONResponse(detail, status_code=400)

        if not code:
            raise HTTPException(status_code=400, detail="Missing code")

        # State check (prevents code replay / wrong flows)
        sess_state = request.session.get("oauth_state")
        request.session.pop("oauth_state", None)  # single-use
        if not sess_state or not state or state != sess_state:
            return JSONResponse({"stage": "authorize", "error": "Invalid state"}, status_code=400)

        # Env sanity again
        prob = _env_problem()
        if prob:
            return JSONResponse({"stage": "env", "error": prob}, status_code=500)

        form = {
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OAUTH_REDIRECT_URI,  # must match exactly
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with httpx.AsyncClient(timeout=10.0) as client:
            tr = await client.post(f"{DISCORD_API}/oauth2/token", data=form, headers=headers)

        if tr.status_code != 200:
            payload = None
            try:
                payload = tr.json()
            except Exception:
                payload = {"raw": tr.text}
            # Common reasons:
            # - code already used/expired
            # - redirect_uri mismatch (exact string mismatch)
            # - wrong client_id/secret
            detail = {
                "stage": "token",
                "status": tr.status_code,
                "discord": payload,
                "hint": "Ensure redirect URI EXACTLY matches in Discord's portal and env, and use fresh /auth/login each time.",
            }
            return JSONResponse(detail, status_code=400 if not debug else 200, headers={"Cache-Control": "no-store"})

        tok = tr.json()
        access_token = tok.get("access_token")
        if not access_token:
            return JSONResponse({"stage": "token", "error": "No access token in response"}, status_code=401)

        auth_hdr = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            ur = await client.get(f"{DISCORD_API}/users/@me", headers=auth_hdr)
            gr = await client.get(f"{DISCORD_API}/users/@me/guilds", headers=auth_hdr)

        if ur.status_code != 200:
            why = None
            try:
                why = ur.json()
            except Exception:
                why = {"raw": ur.text}
            return JSONResponse({"stage": "userinfo", "discord": why}, status_code=401)

        # Success
        request.session.clear()
        request.session["access_token"] = access_token
        request.session["user"] = ur.json()
        if gr.status_code == 200:
            try:
                request.session["guild_ids"] = [str(g["id"]) for g in gr.json() if "id" in g]
            except Exception:
                request.session["guild_ids"] = []

        return RedirectResponse("/")

    @app.get("/auth/logout")
    async def auth_logout(request: Request):
        request.session.clear()
        return RedirectResponse("/")


    # ---------- Helpers ----------
    def _top(gid: int):
        with get_conn() as c:
            rows = c.execute(
                "SELECT user_id, cnt FROM counting_user_counts WHERE guild_id=? ORDER BY cnt DESC LIMIT 10",
                (gid,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def _guild_channels(gid: int):
        chans = []
        if _bot:
            g = _bot.get_guild(gid)
            if g:
                for ch in g.text_channels:
                    chans.append({"id": ch.id, "name": f"#{ch.name}"})
        return chans

    async def _guild_roles(gid: int):
        roles = []
        if _bot:
            g = _bot.get_guild(gid)
            if g:
                for r in g.roles:
                    # skip @everyone and bot-managed roles
                    if r.is_default() or r.is_bot_managed():
                        continue
                    roles.append({"id": r.id, "name": r.name})
        roles.sort(key=lambda x: x["id"], reverse=True)
        return roles

    async def _display_name(gid: int, user_id: int) -> str:
        """Resolve a user's display name for the leaderboard."""
        if not _bot:
            return f"User ID {user_id}"

        g = _bot.get_guild(gid)

        # 1) Try cache
        if g:
            m = g.get_member(user_id)
            if m:
                return m.display_name

        # 2) Try API fetch
        if g:
            try:
                m = await g.fetch_member(user_id)
                if m:
                    return m.display_name
            except Exception:
                pass

        # 3) Fallback to global user
        try:
            u = await _bot.fetch_user(user_id)
            if u:
                return (u.global_name or u.name)
        except Exception:
            pass

        return f"User ID {user_id}"

    def _bot_avatar_url(size: int = 32) -> str:
        """Brand image for the dashboard (brand override → bot avatar → placeholder)."""
        if _brand_avatar_url:
            return _brand_avatar_url
        try:
            if _bot and _bot.user:
                return _bot.user.display_avatar.with_size(size).url
        except Exception:
            pass
        return "https://cdn.discordapp.com/embed/avatars/0.png"

    # ---------- Changelog helpers ----------
    def _find_changelog_file() -> Optional[Path]:
        """Look for changelog.json in a few common spots."""
        candidates = [
            Path("changelog.json"),
            Path("data/changelog.json"),
            Path("templates/changelog.json"),
            Path(__file__).resolve().parent.parent / "changelog.json",  # project root guess
        ]
        for p in candidates:
            if p.is_file():
                return p
        return None

    def _load_changelog() -> list[dict]:
        p = _find_changelog_file()
        if not p:
            return []
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data = [data]
                return data if isinstance(data, list) else []
        except Exception:
            return []

    # ---------- Status helpers ----------
    def _db_ok() -> bool:
        try:
            with get_conn() as c:
                c.execute("SELECT 1")
            return True
        except Exception:
            return False

    def _status_snapshot() -> dict:
        # Discord state
        bot_ok = False
        guilds = 0
        user_str = None
        try:
            if _bot and _bot.user:
                bot_ok = True
                user_str = f"{_bot.user} ({_bot.user.id})"
                guilds = len(_bot.guilds or [])
        except Exception:
            bot_ok = False

        cf_last_check = get_setting(0, "cf_last_check", None)

        return {
            "version": version,
            "uptime_seconds": int(time.time() - _START_TS),
            "discord": {
                "connected": bot_ok,
                "bot_user": user_str,
                "guild_count": guilds,
            },
            "database": {"ok": _db_ok()},
            "dashboard": {
                "host": os.getenv("DASHBOARD_HOST", "127.0.0.1"),
                "port": int(os.getenv("DASHBOARD_PORT", "5500")),
            },
            "curseforge": {
                "enabled": "cogs.Curseforge_updates" in (os.getenv("COGS", "") or ""),
                "last_check_ts": int(cf_last_check) if (cf_last_check and str(cf_last_check).isdigit()) else None,
            },
            "updated_ts": int(time.time()),
        }

    # ---------- Base Styles (modern UI) ----------
    def base_head(title: str) -> str:
        return f"""
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>{title}</title>
          <style>
            :root {{
              --bg: #0b0d10;
              --elev: #12161a;
              --card: #161b22;
              --text: #e6edf3;
              --muted: #9aa4af;
              --border: #2b3440;
              --brand: #58a6ff;
              --brand-2: #7ee787;
              --warn: #ffb86b;
              --danger: #ff6b6b;
              --shadow: 0 8px 24px rgba(0,0,0,.35);
            }}
            @media (prefers-color-scheme: light) {{
              :root {{
                --bg: #f6f7f9;
                --elev: #ffffff;
                --card: #ffffff;
                --text: #0f1720;
                --muted: #546176;
                --border: #e6e9ef;
                --brand: #2563eb;
                --brand-2: #16a34a;
                --shadow: 0 10px 24px rgba(0,0,0,.06);
              }}
            }}
            html, body {{ margin:0; padding:0; }}
            body {{
              font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, Apple Color Emoji, Segoe UI Emoji;
              background: var(--bg);
              color: var(--text);
            }}
            .container {{ max-width: 1100px; margin: 32px auto; padding: 0 20px; }}
            .nav {{
              display:flex; align-items:center; justify-content:space-between;
              margin-bottom: 20px;
            }}
            .brand {{
              font-weight: 700; letter-spacing: .2px; display:flex; align-items:center; gap:10px;
            }}
            .brand .logo {{
              width: 28px; height: 28px; border-radius: 999px; border:1px solid var(--border); object-fit: cover;
              background: var(--elev);
            }}
            .badge {{ font-size:12px; padding:4px 8px; border:1px solid var(--border); border-radius: 999px; color: var(--muted); }}
            .row {{ display:grid; gap:16px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }}
            .card {{
              background: var(--card); border:1px solid var(--border);
              border-radius:16px; padding:16px; box-shadow: var(--shadow);
            }}
            .card h2 {{ margin: 0 0 8px 0; font-size: 18px; }}
            .muted {{ color: var(--muted); }}
            a {{ color: var(--brand); text-decoration: none; }}
            a.button, button.button {{
              display:inline-flex; align-items:center; gap:8px;
              background: linear-gradient(180deg, var(--brand), #3b82f6);
              color:white; padding:10px 14px; border-radius:10px; border:none; cursor:pointer;
              box-shadow: 0 6px 18px rgba(37,99,235,.3);
              transition: transform .06s ease;
            }}
            a.button:hover, button.button:hover {{ transform: translateY(-1px); }}
            a.button.secondary, button.secondary {{
              background: linear-gradient(180deg, #2e2e2e, #1c1c1c);
              color: var(--text); border:1px solid var(--border); box-shadow:none;
            }}
            .grid {{ display:grid; gap:16px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
            .card-link {{ display:block; padding:16px; border-radius:14px; background:var(--card); border:1px solid var(--border); transition: transform .06s ease, border-color .1s; }}
            .card-link:hover {{ transform: translateY(-2px); border-color: var(--brand); }}
            .kv {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
            label {{ display:block; margin:10px 0 6px; font-weight:600; }}
            input, select {{
              width:100%; padding:10px 12px; border-radius:10px; border:1px solid var(--border);
              background: var(--elev); color: var(--text);
            }}
            .btn-row {{ display:flex; gap:10px; flex-wrap:wrap; margin-top: 12px; }}
            table {{ width:100%; border-collapse: collapse; }}
            th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--border); }}
            th {{ font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
            .footer {{ margin-top: 28px; color: var(--muted); font-size: 13px; text-align:center; }}
            .toggle {{ display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border-radius:999px; border:1px solid var(--border); background:var(--elev); color: var(--muted); cursor:pointer; }}
          </style>
          <script>
            (function(){{
              const k='cg-theme';
              const saved = localStorage.getItem(k);
              if(saved) {{ document.documentElement.dataset.theme = saved; }}
              window.toggleTheme = function(){{
                const cur = document.documentElement.dataset.theme || '';
                const next = cur==='light' ? '' : 'light';
                document.documentElement.dataset.theme = next;
                localStorage.setItem(k, next);
              }}
            }})();
          </script>
        </head>
        """

    def page_shell(title: str, header_right: str, body: str, version_str: str, avatar_url: str) -> str:
        return f"""
        <html>
          {base_head(title)}
          <body>
            <div class="container">
              <div class="nav">
                <div class="brand">
                  <img class="logo" src="{avatar_url}" alt="Bot avatar" />
                  CelestiGuard <span class="badge">v{version_str}</span>
                </div>
                <div class="kv">
                  {header_right}
                  <span class="toggle" onclick="toggleTheme()">🌓 Theme</span>
                </div>
              </div>
              {body}
              <div class="footer">CelestiGuard v{version_str}</div>
            </div>
          </body>
        </html>
        """

    # ---------- Public, health, changelog ----------
    @app.get("/health")
    async def health():
        return JSONResponse({"ok": True, "version": version})

    @app.get("/api/version")
    async def api_version():
        return JSONResponse({"version": version})

    @app.get("/api/changelog")
    async def api_changelog():
        """
        Always return a JSON list (possibly empty) and disable caching so the page
        never gets stuck on stale responses.
        """
        data = _load_changelog() or []
        return JSONResponse(
            data,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @app.get("/changelog", response_class=HTMLResponse)
    async def changelog_page():
        # NOTE: NOT an f-string, so ${...} is left for JS template literals.
        body = """
        <div class="row" style="grid-template-columns:1fr">
          <div class="card">
            <h2>Changelog</h2>
            <div id="cl">Loading…</div>
          </div>
        </div>
        <script>
          (async function(){
            const el = document.getElementById('cl');
            try {
              const res = await fetch('/api/changelog', {
                cache: 'no-store',
                headers: { 'Cache-Control': 'no-store' }
              });
              if (!res.ok) {
                el.textContent = 'Failed to load changelog.';
                return;
              }
              let items = [];
              try { items = await res.json(); } catch { items = []; }

              if (!Array.isArray(items) || items.length === 0) {
                el.textContent = 'No changelog entries yet.';
                return;
              }

              el.innerHTML = items.map(entry => `
                <div class="card" style="margin-top:12px">
                  <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
                    <div><strong>${entry.version || 'unversioned'}</strong></div>
                    <div class="muted">${entry.date || ''}</div>
                  </div>
                  <ul style="margin:10px 0 0 18px">
                    ${(entry.changes || []).map(c => `<li>${c}</li>`).join('')}
                  </ul>
                </div>
              `).join('');
            } catch (_e) {
              el.textContent = 'Failed to load changelog.';
            }
          })();
        </script>
        """
        return HTMLResponse(page_shell("Changelog • CelestiGuard", "", body, version, _bot_avatar_url(28)))

        # ---------- Status API & Page (public) ----------
    @app.get("/api/status")
    async def api_status():
        return JSONResponse(_status_snapshot())

    @app.get("/status", response_class=HTMLResponse)
    async def status_page():
        # NOTE: body is a plain string (NOT an f-string), so { } are safe for JS.
        body = """
          <div class="row" style="grid-template-columns:1fr">
            <div class="card">
              <h2>Status</h2>
              <div class="muted" style="margin-bottom:8px">
                Uptime: <b id="uptime">loading…</b>
                • Version: <b id="version">loading…</b>
              </div>

              <div class="card" style="margin-top:12px">
                <h3 style="margin:0 0 8px 0">Affected</h3>
                <ul style="margin:0 0 0 18px">
                  <li><b>Discord Gateway:</b>
                    <span id="discord-status" class="status-pill">loading…</span>
                  </li>
                  <li><b>Database:</b>
                    <span id="db-status" class="status-pill">loading…</span>
                  </li>
                  <li><b>Dashboard API:</b>
                    <span id="dashboard-status" class="status-pill">loading…</span>
                  </li>
                  <li><b>CurseForge Monitor:</b>
                    <span id="cf-enabled" class="status-pill">loading…</span>
                  </li>
                </ul>
              </div>

              <div class="card" style="margin-top:12px">
                <h3 style="margin:0 0 8px 0">Updated</h3>
                <div class="muted" id="updated-ts">loading…</div>
              </div>

              <div class="card" style="margin-top:12px">
                <h3 style="margin:0 0 8px 0">Details</h3>
                <div class="muted">
                  Discord: <span id="discord-user">loading…</span>
                  • Guilds: <span id="discord-guilds">–</span>
                </div>
                <div class="muted">
                  Dashboard: <span id="dash-hostport">loading…</span>
                </div>
                <div class="muted">
                  CurseForge last check: <span id="cf-last">–</span>
                </div>
              </div>

              <div class="card" style="margin-top:12px">
                <h3 style="margin:0 0 8px 0">Last Error</h3>
                <div id="last-error-body" class="muted">No recent errors.</div>
              </div>
            </div>
          </div>

          <script>
            function setStatusPill(id, ok, labelOk, labelBad) {
              const el = document.getElementById(id);
              if (!el) return;
              el.textContent = ok ? (labelOk || 'OK') : (labelBad || 'Issue');
              el.classList.remove('status-ok', 'status-bad', 'status-warn');
              el.classList.add(ok ? 'status-ok' : 'status-bad');
            }

            async function refreshStatus() {
              try {
                const r = await fetch('/api/status', { cache: 'no-store' });
                if (!r.ok) return;
                const s = await r.json();

                // Uptime & version
                const uptime = document.getElementById('uptime');
                const version = document.getElementById('version');
                if (uptime && typeof s.uptime_seconds === 'number') {
                  uptime.textContent = s.uptime_seconds + 's';
                }
                if (version && s.version) {
                  version.textContent = s.version;
                }

                // Affected states
                const discordConnected = !!(s.discord && s.discord.connected);
                const dbOk = !!(s.database && s.database.ok);
                const cfEnabled = !!(s.curseforge && s.curseforge.enabled);

                setStatusPill('discord-status', discordConnected, 'Up', 'Issue');
                setStatusPill('db-status', dbOk, 'Up', 'Issue');
                setStatusPill('cf-enabled', cfEnabled, 'Enabled', 'Disabled');

                // Dashboard API is considered OK if this request succeeded
                setStatusPill('dashboard-status', true, 'Up', 'Issue');

                // Updated timestamp
                const updated = document.getElementById('updated-ts');
                if (updated && typeof s.updated_ts === 'number') {
                  const d = new Date(s.updated_ts * 1000).toUTCString().replace('GMT', 'UTC');
                  updated.textContent = d;
                }

                // Details
                const dUser = document.getElementById('discord-user');
                const dGuilds = document.getElementById('discord-guilds');
                const dashHP = document.getElementById('dash-hostport');
                const cfLast = document.getElementById('cf-last');

                if (dUser) {
                  dUser.textContent = (s.discord && s.discord.bot_user) || '—';
                }
                if (dGuilds) {
                  dGuilds.textContent = (s.discord && s.discord.guild_count != null)
                    ? s.discord.guild_count
                    : '0';
                }
                if (dashHP) {
                  const h = (s.dashboard && s.dashboard.host) || '—';
                  const p = (s.dashboard && s.dashboard.port) || '—';
                  dashHP.textContent = h + ':' + p;
                }
                if (cfLast) {
                  if (s.curseforge && s.curseforge.last_check_ts) {
                    const d = new Date(s.curseforge.last_check_ts * 1000).toUTCString().replace('GMT', 'UTC');
                    cfLast.textContent = d;
                  } else {
                    cfLast.textContent = '—';
                  }
                }

                // Last error
                const errBox = document.getElementById('last-error-body');
                if (errBox) {
                  const le = s.last_error;
                  if (le && le.message) {
                    const comp = le.component || 'unknown';
                    errBox.textContent = '[' + comp + '] ' + le.message;
                    errBox.classList.remove('status-ok');
                    errBox.classList.add('status-bad');
                  } else {
                    errBox.textContent = 'No recent errors.';
                    errBox.classList.remove('status-bad');
                  }
                }

              } catch (e) {
                console.error('status refresh failed', e);
              }
            }

            // Initial load + refresh every 30s
            refreshStatus();
            setInterval(refreshStatus, 30000);
          </script>
        """
        return HTMLResponse(page_shell("Status • CelestiGuard", "", body, version, _bot_avatar_url(28)))

    @app.get("/guild/{gid}", response_class=HTMLResponse)
    async def guild_view(
        gid: int,
        request: Request,
        _auth: bool = Depends(require_user),
        _member: bool = Depends(_member_dep),
    ):
        st = get_state(gid)
        extreme = (get_setting(gid, "extreme_mode", "false") == "true")
        delete_wrong = (get_setting(gid, "delete_wrong", "true") == "true")
        top = _top(gid)
        channels = await _guild_channels(gid)
        roles = await _guild_roles(gid)
        cfg = get_guild_config(gid)

        # Resolve guild name
        g_name = None
        if _bot:
            gobj = _bot.get_guild(gid)
            if gobj:
                g_name = gobj.name

        ch_name = None
        if _bot:
            g = _bot.get_guild(gid)
            if g and st.get("channel_id"):
                ch = g.get_channel(st["channel_id"])
                ch_name = f"#{getattr(ch,'name','?')}" if ch else None

        # selects
        options = "<option value=''>— no change —</option>" + "".join(
            f"<option value='{ch['id']}'{' selected' if st.get('channel_id')==ch['id'] else ''}>{ch['name']}</option>"
            for ch in channels
        )
        log_opts = "<option value=''>— disabled —</option>" + "".join(
            f"<option value='{ch['id']}'{' selected' if cfg.get('log_channel_id')==ch['id'] else ''}>{ch['name']}</option>"
            for ch in channels
        )
        wel_opts = "<option value=''>— disabled —</option>" + "".join(
            f"<option value='{ch['id']}'{' selected' if cfg.get('welcome_channel_id')==ch['id'] else ''}>{ch['name']}</option>"
            for ch in channels
        )
        role_opts = "<option value=''>— none —</option>" + "".join(
            f"<option value='{r['id']}'{' selected' if cfg.get('autorole_id')==r['id'] else ''}>{r['name']}</option>"
            for r in roles
        )
        welcome_msg = (cfg.get("welcome_message") or "Welcome {mention}!")

        # Resolve names for leaderboard
        name_tasks = [_display_name(gid, int(r["user_id"])) for r in top]
        names = await asyncio.gather(*name_tasks) if name_tasks else []
        lb_rows = "".join([f"<tr><td>{i+1}</td><td>{nm}</td><td style='text-align:right'>{r['cnt']}</td></tr>"
                           for i, (r, nm) in enumerate(zip(top, names))]) or "<tr><td colspan='3' class='muted'>No data</td></tr>"

        header_right = f"<a class='button secondary' href='/'>← Back</a>"

        body = f"""
          <div class="row">
            <div class="card" style="grid-column:1/-1">
              <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap">
                <div>
                  <h2 style="margin:0">{g_name or ('Guild ' + str(gid))}</h2>
                  <div class="muted">ID: {gid}</div>
                </div>
                <div class="kv">
                  <span class="badge">{'Extreme Mode ON' if extreme else 'Extreme Mode OFF'}</span>
                  <span class="badge">{'Auto-delete ON' if delete_wrong else 'Auto-delete OFF'}</span>
                </div>
              </div>
            </div>
          </div>

          <div class="row">
            <div class="card">
              <h2>Counting</h2>
              <div class="muted" style="margin-bottom:8px">Channel: {ch_name or st.get("channel_id") or "not set"}</div>
              <div class="kv" style="margin-bottom:10px">
                <div>Current: <b>{st["last_number"]}</b></div>
                <div>Next: <b>{(st["last_number"] or 0)+1}</b></div>
              </div>
              <form method='post' action='/guild/{gid}/counting'>
                <label>Channel</label>
                <select name='channel_id'>{options}</select>
                <label>Set Count</label>
                <input type='number' name='set_count' placeholder='42'>
                <div class='btn-row'>
                  <button class="button" type='submit'>Save</button>
                  <button class="button secondary" type='submit' name='reset' value='1'>Reset</button>
                  <button class="button secondary" type='submit' name='synccount' value='1'>Sync from History</button>
                </div>
              </form>
            </div>

            <div class="card">
              <h2>Settings</h2>
              <form method='post' action='/guild/{gid}/settings'>
                <label><input type='checkbox' name='extreme_mode' {"checked" if extreme else ""}> Extreme Mode</label>
                <label><input type='checkbox' name='delete_wrong' {"checked" if delete_wrong else ""}> Delete wrong messages</label>
                <div class='btn-row'><button class="button" type='submit'>Update</button></div>
              </form>
            </div>

            <div class="card">
              <h2>Leaderboard</h2>
              <table>
                <thead><tr><th>#</th><th>User</th><th style="text-align:right">Count</th></tr></thead>
                <tbody>{lb_rows}</tbody>
              </table>
            </div>

            <div class="card">
              <h2>Server Management</h2>
              <form method="post" action="/guild/{gid}/servercfg">
                <label>Log Channel</label>
                <select name="log_channel_id">{log_opts}</select>

                <label>Welcome Channel</label>
                <select name="welcome_channel_id">{wel_opts}</select>

                <label>Welcome Message</label>
                <input type="text" name="welcome_message" value="{welcome_msg.replace('"','&quot;')}" placeholder="Welcome {{mention}}!">

                <label>Autorole</label>
                <select name="autorole_id">{role_opts}</select>

                <div class="muted" style="margin-top:6px">
                  Tip: use <code>{{{{mention}}}}</code> or <code>{{{{user}}}}</code> in the welcome message.
                </div>

                <div class="btn-row" style="margin-top:10px">
                  <button class="button" type="submit">Save</button>
                </div>
              </form>
            </div>
          </div>
        """

        return HTMLResponse(page_shell(g_name or (f"Guild {gid}"), header_right, body, version, _bot_avatar_url(28)))

    @app.post("/guild/{gid}/settings")
    async def update_settings(
        gid: int,
        request: Request,
        extreme_mode: str | None = Form(None),
        delete_wrong: str | None = Form(None),
        _auth: bool = Depends(require_user),
        _member: bool = Depends(_member_dep),
    ):
        set_setting(gid, "extreme_mode", "true" if extreme_mode == "on" else "false")
        set_setting(gid, "delete_wrong", "true" if delete_wrong == "on" else "false")
        return RedirectResponse(url=f"/guild/{gid}", status_code=HTTP_303_SEE_OTHER)

    @app.post("/guild/{gid}/counting")
    async def update_counting(
        gid: int,
        request: Request,
        channel_id: Optional[str] = Form(None),
        set_count: Optional[str] = Form(None),
        reset: Optional[str] = Form(None),
        synccount: Optional[str] = Form(None),
        _auth: bool = Depends(require_user),
        _member: bool = Depends(_member_dep),
    ):
        if channel_id:
            set_state(gid, channel_id=int(channel_id))
        if set_count is not None and set_count != "":
            set_state(gid, last_number=max(0, int(set_count)), last_user_id=None)
        if reset is not None:
            set_state(gid, last_number=0, last_user_id=None)
        if synccount is not None and _bot is not None:
            g = _bot.get_guild(gid)
            if g:
                st = get_state(gid)
                ch = g.get_channel(st.get("channel_id"))
                if ch:
                    from cogs.counting import backfill_from_history, get_extreme_mode
                    extreme = get_extreme_mode(gid)
                    last_num, last_user = await backfill_from_history(ch, extreme)
                    set_state(gid, last_number=last_num, last_user_id=last_user)
        return RedirectResponse(url=f"/guild/{gid}", status_code=HTTP_303_SEE_OTHER)

    @app.post("/guild/{gid}/servercfg")
    async def save_server_cfg(
        gid: int,
        request: Request,
        log_channel_id: Optional[str] = Form(None),
        welcome_channel_id: Optional[str] = Form(None),
        welcome_message: Optional[str] = Form(None),
        autorole_id: Optional[str] = Form(None),
        _auth: bool = Depends(require_user),
        _member: bool = Depends(_member_dep),
    ):
        def _to_int_or_none(v: Optional[str]):
            try:
                return int(v) if v not in (None, "", "None") else None
            except Exception:
                return None

        set_guild_config(
            gid,
            log_channel_id=_to_int_or_none(log_channel_id),
            welcome_channel_id=_to_int_or_none(welcome_channel_id),
            welcome_message=(welcome_message or "").strip() or None,
            autorole_id=_to_int_or_none(autorole_id),
        )
        return RedirectResponse(url=f"/guild/{gid}", status_code=HTTP_303_SEE_OTHER)

    return app