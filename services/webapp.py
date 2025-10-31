from __future__ import annotations
import os, time, secrets, asyncio, json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.status import HTTP_303_SEE_OTHER

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

    # ---------- Auth ----------
    async def validate_ephemeral_token(token: str, path_guild_id: int | None) -> bool:
        if not token:
            return False
        now = int(time.time())
        with get_conn() as c:
            row = c.execute(
                "SELECT token, guild_id, expires_ts, used FROM ephemeral_tokens WHERE token=?",
                (token,),
            ).fetchone()
            if not row:
                return False
            if row["used"] or row["expires_ts"] < now:
                return False
            gid_lock = row["guild_id"]
            if gid_lock is not None and path_guild_id is not None and gid_lock != path_guild_id:
                return False
            c.execute("UPDATE ephemeral_tokens SET used=1 WHERE token=?", (token,))
            return True

    def require_token(request: Request, gid_in_path: int | None = None):
        """
        Accepts either:
          - permanent token:   ?token=...  or  Authorization: Bearer <TOKEN>
          - one-time token:    ?ot=...     or  Authorization: Bearer ot:<token>
        """
        PERM_TOKEN = os.getenv("DASHBOARD_TOKEN", "")

        qtok = request.query_params.get("token")
        otok = request.query_params.get("ot")
        h = request.headers.get("Authorization", "")
        ht = h.split(" ", 1)[-1] if " " in h else h

        # permanent token
        if PERM_TOKEN and (qtok == PERM_TOKEN or ht == PERM_TOKEN):
            return True

        # ephemeral token via header or query (header form: Authorization: Bearer ot:<token>)
        if ht.startswith("ot:"):
            otok = ht[3:]
        if otok:
            try:
                if gid_in_path is None:
                    gid_str = request.path_params.get("gid") if hasattr(request, "path_params") else None
                    gid_in_path = int(gid_str) if gid_str else None
            except Exception:
                gid_in_path = None
            ok = asyncio.get_event_loop().run_until_complete(validate_ephemeral_token(otok, gid_in_path))
            if ok:
                return True

        if not PERM_TOKEN:
            raise HTTPException(status_code=401, detail="Dashboard disabled (no token set)")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # helper for FastAPI Depends that includes the path gid
    def _require_token_with_gid(request: Request, gid: int):
        return require_token(request, gid)

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
                    # skip @everyone (managed by guild) and bot-managed integration roles
                    if r.is_default() or r.is_bot_managed():
                        continue
                    roles.append({"id": r.id, "name": r.name})
        # highest first looks nicer
        roles.sort(key=lambda x: x["id"], reverse=True)
        return roles

    async def _display_name(gid: int, user_id: int) -> str:
        """Resolve a user's display name for the leaderboard."""
        if not _bot:
            return f"User ID {user_id}"

        g = _bot.get_guild(gid)

        # 1) Try cache (fast)
        if g:
            m = g.get_member(user_id)
            if m:
                return m.display_name

        # 2) Try API fetch for member
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
                # Normalize to a list of entries
                if isinstance(data, dict):
                    data = [data]
                return data if isinstance(data, list) else []
        except Exception:
            return []

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
            .toggle {{ display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border-radius:999px; border:1px solid var(--border); background:var(--elev); color:var(--muted); cursor:pointer; }}
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

    # ---------- Public, health, and changelog ----------
    @app.get("/health")
    async def health():
        return JSONResponse({"ok": True, "version": version})

    @app.get("/api/version")
    async def api_version():
        return JSONResponse({"version": version})

    @app.get("/api/changelog")
    async def api_changelog():
        data = _load_changelog()
        if not data:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(data)

    @app.get("/changelog", response_class=HTMLResponse)
    async def changelog_page():
        # Lightweight page that fetches /api/changelog and renders
        body = f"""
        <div class="row" style="grid-template-columns:1fr">
          <div class="card">
            <h2>Changelog</h2>
            <div id="cl">Loading…</div>
          </div>
        </div>
        <script>
          (async function(){{
            const el = document.getElementById('cl');
            try {{
              const res = await fetch('/api/changelog', {{cache:'no-store'}});
              if (!res.ok) throw new Error('not ok');
              const items = await res.json();
              if (!Array.isArray(items) || items.length===0) {{
                el.textContent = 'No changelog entries yet.';
                return;
              }}
              el.innerHTML = items.map(entry => `
                <div class="card" style="margin-top:12px">
                  <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
                    <div><strong>${{entry.version || 'unversioned'}}</strong></div>
                    <div class="muted">${{entry.date || ''}}</div>
                  </div>
                  <ul style="margin:10px 0 0 18px">
                    ${(entry.changes || []).map(c => `<li>${{c}}</li>`).join('')}
                  </ul>
                </div>
              `).join('');
            }} catch (e) {{
              el.textContent = 'Failed to load changelog.';
            }}
          }})();
        </script>
        """
        return HTMLResponse(page_shell("Changelog • CelestiGuard", "", body, version, _bot_avatar_url(28)))

    # ---------- Private (token-protected) dashboard ----------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, _: bool = Depends(require_token)):
        items = []
        if _bot and _bot.guilds:
            for g in _bot.guilds:
                items.append(f"""
                <a class="card-link" href='/guild/{g.id}?token={request.query_params.get("token","")}'>
                  <div style="font-weight:700; font-size:16px; margin-bottom:4px">{g.name}</div>
                  <div class="muted">ID: {g.id} • Members: {getattr(g, 'member_count', '—')}</div>
                </a>""")
        body = f"""
          <div class="row">
            <div class="card" style="grid-column:1/-1">
              <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap">
                <div>
                  <h2 style="margin:0 0 4px 0">Dashboard</h2>
                  <div class="muted">Manage counting channels, sync, and settings.</div>
                </div>
                <div class="kv">
                  <a class="button secondary" href="https://discord.com/developers/applications" target="_blank" rel="noreferrer">Open Dev Portal</a>
                  <a class="button" href="/changelog" target="_blank" rel="noreferrer">Changelog</a>
                </div>
              </div>
            </div>
          </div>
          <div class="grid" style="margin-top:16px">
            {''.join(items) if items else '<div class="muted">No guilds yet. Invite the bot.</div>'}
          </div>
        """
        return HTMLResponse(page_shell("CelestiGuard", "", body, version, _bot_avatar_url(28)))

    @app.get("/guild/{gid}", response_class=HTMLResponse)
    async def guild_view(gid: int, request: Request, _: bool = Depends(require_token)):
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

        header_right = f"<a class='button secondary' href='/?token={request.query_params.get('token','')}'>← Back</a>"

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
              <form method='post' action='/guild/{gid}/counting?token={request.query_params.get("token","")}'>
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
              <form method='post' action='/guild/{gid}/settings?token={request.query_params.get("token","")}'>
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
              <form method="post" action="/guild/{gid}/servercfg?token={request.query_params.get('token','')}">
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

            <div class="card">
              <h2>Secure Share</h2>
              <p class="muted">Create a <b>single-use</b>, <b>time-limited</b> link for this guild page.</p>
              <form method="post" action="/guild/{gid}/share?token={request.query_params.get('token','')}">
                <label>TTL (seconds)</label>
                <input type="number" name="ttl" value="900" min="60">
                <div class="btn-row"><button class="button" type="submit">Generate one-time link</button></div>
              </form>
            </div>
          </div>
        """

        return HTMLResponse(page_shell(g_name or (f"Guild {gid}"), header_right, body, version, _bot_avatar_url(28)))

    @app.post("/guild/{gid}/settings")
    async def update_settings(gid: int, request: Request, extreme_mode: str | None = Form(None),
                              delete_wrong: str | None = Form(None), _: bool = Depends(require_token)):
        set_setting(gid, "extreme_mode", "true" if extreme_mode == "on" else "false")
        set_setting(gid, "delete_wrong", "true" if delete_wrong == "on" else "false")
        return RedirectResponse(url=f"/guild/{gid}?token={request.query_params.get('token','')}",
                                status_code=HTTP_303_SEE_OTHER)

    @app.post("/guild/{gid}/counting")
    async def update_counting(gid: int, request: Request, channel_id: Optional[str] = Form(None),
                              set_count: Optional[str] = Form(None), reset: Optional[str] = Form(None),
                              synccount: Optional[str] = Form(None), _: bool = Depends(require_token)):
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
        return RedirectResponse(url=f"/guild/{gid}?token={request.query_params.get('token','')}",
                                status_code=HTTP_303_SEE_OTHER)

    @app.post("/guild/{gid}/servercfg")
    async def save_server_cfg(
        gid: int,
        request: Request,
        log_channel_id: Optional[str] = Form(None),
        welcome_channel_id: Optional[str] = Form(None),
        welcome_message: Optional[str] = Form(None),
        autorole_id: Optional[str] = Form(None),
        _: bool = Depends(require_token),
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
        return RedirectResponse(url=f"/guild/{gid}?token={request.query_params.get('token','')}",
                                status_code=HTTP_303_SEE_OTHER)

    @app.post("/guild/{gid}/share")
    async def make_share_token(gid: int, request: Request, ttl: int = Form(900),
                               _: bool = Depends(_require_token_with_gid)):
        tok = secrets.token_urlsafe(24)
        exp = int(time.time()) + int(ttl)
        with get_conn() as c:
            c.execute(
                "INSERT INTO ephemeral_tokens(token, guild_id, expires_ts, used, created_ts) VALUES (?,?,?,?,?)",
                (tok, gid, exp, 0, int(time.time()))
            )
        url = f"/guild/{gid}?ot_created=1&expires={exp}&ot_preview={tok}"
        return RedirectResponse(url=url, status_code=HTTP_303_SEE_OTHER)

    return app
