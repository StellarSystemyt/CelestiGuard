<<<<<<< HEAD
from __future__ import annotations
import os, time, secrets, asyncio, html, json
from typing import Optional
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.status import HTTP_303_SEE_OTHER
from services.db import (
    get_conn, init, get_state, set_state,
    get_setting, set_setting, get_guild_config, set_guild_config
)

__all__ = ["create_app", "set_bot", "set_brand_avatar"]

_bot = None
_brand_avatar_url: str | None = None

def set_bot(bot):
    global _bot
    _bot = bot

def set_brand_avatar(url: str | None):
    global _brand_avatar_url
    _brand_avatar_url = url


def create_app(version: str = "dev") -> FastAPI:
    init()
    app = FastAPI(title="CelestiGuard Dashboard")

    # ---------------- AUTH ----------------
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
        PERM_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
        qtok = request.query_params.get("token")
        otok = request.query_params.get("ot")
        h = request.headers.get("Authorization", "")
        ht = h.split(" ", 1)[-1] if " " in h else h

        if PERM_TOKEN and (qtok == PERM_TOKEN or ht == PERM_TOKEN):
            return True

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

    def _require_token_with_gid(request: Request, gid: int):
        return require_token(request, gid)

    # ---------------- HELPERS ----------------
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
                    if r.is_default() or r.is_bot_managed():
                        continue
                    roles.append({"id": r.id, "name": r.name})
        roles.sort(key=lambda x: x["id"], reverse=True)
        return roles

    async def _display_name(gid: int, user_id: int) -> str:
        if not _bot:
            return f"User ID {user_id}"
        g = _bot.get_guild(gid)
        if g:
            m = g.get_member(user_id)
            if m:
                return m.display_name
        if g:
            try:
                m = await g.fetch_member(user_id)
                if m:
                    return m.display_name
            except Exception:
                pass
        try:
            u = await _bot.fetch_user(user_id)
            if u:
                return u.global_name or u.name
        except Exception:
            pass
        return f"User ID {user_id}"

    def _bot_avatar_url(size: int = 32) -> str:
        if _brand_avatar_url:
            return _brand_avatar_url
        try:
            if _bot and _bot.user:
                return _bot.user.display_avatar.with_size(size).url
        except Exception:
            pass
        return "https://cdn.discordapp.com/embed/avatars/0.png"

    # ---------------- UI Helpers ----------------
    def base_head(title: str) -> str:
        return f"""
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>{title}</title>
          <style>
            body {{ background: #0b0d10; color: #e6edf3; font-family: system-ui; margin: 0; padding: 20px; }}
            .container {{ max-width: 1000px; margin: auto; }}
            .nav {{ display:flex; justify-content:space-between; align-items:center; }}
            .logo {{ width:32px; height:32px; border-radius:50%; }}
            .card {{ background:#161b22; padding:16px; border-radius:12px; margin-bottom:12px; }}
            a {{ color:#58a6ff; text-decoration:none; }}
            .muted {{ color:#9aa4af; }}
            .badge {{ font-size:12px; border:1px solid #2b3440; border-radius:999px; padding:2px 6px; color:#9aa4af; }}
          </style>
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
                  <img class="logo" src="{avatar_url}" alt="avatar"/>
                  <strong>CelestiGuard</strong> <span class="badge">v{version_str}</span>
                </div>
                <div>{header_right}</div>
              </div>
              {body}
              <p class="muted" style="text-align:center;margin-top:20px;">CelestiGuard v{version_str}</p>
            </div>
          </body>
        </html>
        """

    # ---------------- ROUTES ----------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, _: bool = Depends(require_token)):
        items = []
        if _bot and _bot.guilds:
            for g in _bot.guilds:
                items.append(f"<a href='/guild/{g.id}?token={request.query_params.get('token','')}'>{g.name}</a><br>")
        body = f"""
        <div class="card">
          <h2>Dashboard</h2>
          <p class="muted">Manage CelestiGuard settings and servers.</p>
          {''.join(items) if items else '<p>No guilds yet.</p>'}
        </div>
        <div class="card">
          <h2>Changelog</h2>
          <p><a href="/api/changelog">View JSON changelog</a></p>
        </div>
        """
        return HTMLResponse(page_shell("CelestiGuard", "", body, version, _bot_avatar_url(28)))

    @app.get("/api/changelog", response_class=JSONResponse)
    async def changelog_api():
        path = os.path.join(os.path.dirname(__file__), "changelog.json")
        if not os.path.exists(path):
            return {"error": "Changelog not found"}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data

    return app
=======
from __future__ import annotations
import os, time, secrets, asyncio, html, json
from typing import Optional
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.status import HTTP_303_SEE_OTHER
from services.db import (
    get_conn, init, get_state, set_state,
    get_setting, set_setting, get_guild_config, set_guild_config
)

__all__ = ["create_app", "set_bot", "set_brand_avatar"]

_bot = None
_brand_avatar_url: str | None = None

def set_bot(bot):
    global _bot
    _bot = bot

def set_brand_avatar(url: str | None):
    global _brand_avatar_url
    _brand_avatar_url = url


def create_app(version: str = "dev") -> FastAPI:
    init()
    app = FastAPI(title="CelestiGuard Dashboard")

    # ---------------- AUTH ----------------
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
        PERM_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
        qtok = request.query_params.get("token")
        otok = request.query_params.get("ot")
        h = request.headers.get("Authorization", "")
        ht = h.split(" ", 1)[-1] if " " in h else h

        if PERM_TOKEN and (qtok == PERM_TOKEN or ht == PERM_TOKEN):
            return True

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

    def _require_token_with_gid(request: Request, gid: int):
        return require_token(request, gid)

    # ---------------- HELPERS ----------------
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
                    if r.is_default() or r.is_bot_managed():
                        continue
                    roles.append({"id": r.id, "name": r.name})
        roles.sort(key=lambda x: x["id"], reverse=True)
        return roles

    async def _display_name(gid: int, user_id: int) -> str:
        if not _bot:
            return f"User ID {user_id}"
        g = _bot.get_guild(gid)
        if g:
            m = g.get_member(user_id)
            if m:
                return m.display_name
        if g:
            try:
                m = await g.fetch_member(user_id)
                if m:
                    return m.display_name
            except Exception:
                pass
        try:
            u = await _bot.fetch_user(user_id)
            if u:
                return u.global_name or u.name
        except Exception:
            pass
        return f"User ID {user_id}"

    def _bot_avatar_url(size: int = 32) -> str:
        if _brand_avatar_url:
            return _brand_avatar_url
        try:
            if _bot and _bot.user:
                return _bot.user.display_avatar.with_size(size).url
        except Exception:
            pass
        return "https://cdn.discordapp.com/embed/avatars/0.png"

    # ---------------- UI Helpers ----------------
    def base_head(title: str) -> str:
        return f"""
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>{title}</title>
          <style>
            body {{ background: #0b0d10; color: #e6edf3; font-family: system-ui; margin: 0; padding: 20px; }}
            .container {{ max-width: 1000px; margin: auto; }}
            .nav {{ display:flex; justify-content:space-between; align-items:center; }}
            .logo {{ width:32px; height:32px; border-radius:50%; }}
            .card {{ background:#161b22; padding:16px; border-radius:12px; margin-bottom:12px; }}
            a {{ color:#58a6ff; text-decoration:none; }}
            .muted {{ color:#9aa4af; }}
            .badge {{ font-size:12px; border:1px solid #2b3440; border-radius:999px; padding:2px 6px; color:#9aa4af; }}
          </style>
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
                  <img class="logo" src="{avatar_url}" alt="avatar"/>
                  <strong>CelestiGuard</strong> <span class="badge">v{version_str}</span>
                </div>
                <div>{header_right}</div>
              </div>
              {body}
              <p class="muted" style="text-align:center;margin-top:20px;">CelestiGuard v{version_str}</p>
            </div>
          </body>
        </html>
        """

    # ---------------- ROUTES ----------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, _: bool = Depends(require_token)):
        items = []
        if _bot and _bot.guilds:
            for g in _bot.guilds:
                items.append(f"<a href='/guild/{g.id}?token={request.query_params.get('token','')}'>{g.name}</a><br>")
        body = f"""
        <div class="card">
          <h2>Dashboard</h2>
          <p class="muted">Manage CelestiGuard settings and servers.</p>
          {''.join(items) if items else '<p>No guilds yet.</p>'}
        </div>
        <div class="card">
          <h2>Changelog</h2>
          <p><a href="/api/changelog">View JSON changelog</a></p>
        </div>
        """
        return HTMLResponse(page_shell("CelestiGuard", "", body, version, _bot_avatar_url(28)))

    @app.get("/api/changelog", response_class=JSONResponse)
    async def changelog_api():
        path = os.path.join(os.path.dirname(__file__), "changelog.json")
        if not os.path.exists(path):
            return {"error": "Changelog not found"}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data

    return app
>>>>>>> dd54628 (start project)
