from __future__ import annotations
import asyncio
import datetime as dt
from typing import Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from services.db import get_conn, init as db_init

API_BASE = "https://api.curseforge.com/v1"

#----------------UI helpers--------------------------

def _discord_ts_iso(ts: str | None) -> Optional[int]:
    if not ts:
        return None
    try:
        when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(when.timestamp())
    except Exception:
        return None

def _short(text: str, limit: int = 700) -> str:
    if not text:
        return ""
    text = text.strip()
    return (text[: limit - 1]+ "…") if len(text) > limit else text

class CFButtons(discord.ui.view):
    def __init__(self, download_url: str, project_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Download", url=download_url))
        self.add_item(discord.ui.Button(label="Project Page", url=project_url))

def build_status_embed(file: dict, project_id: int, project_name: Optional[str]) -> discord.Embed:
    file_name = file.get("displayName") or file.get("fileName") or "New file"
    download_url = file.get("downloadUrl") or file.get("fileUrl") or f"https://www.curseforge.com/projects/{project_id}"
    project_url = f"https://www.curseforge.com/projects/{project_id}"

    versions = ", ".join(file.get("gameVersions") or []) or "-"
    rel_type = {1: "Release", 2: "Beta", 3: "Alpha"}.get(file.get("releaseType"), "Release")
    iso = file.get("fileDate")
    ts = _discord_ts_iso(iso)

    notes_raw = (file.get("changelog") or "").replace("<br>", "\n").replace("<br/>", "<\n>")
    notes = _short(notes_raw, 700)

    embed = discord.Embed(
        title=file_name,
        url=download_url,
        description=notes or "No changelog provided.",
        color=discord.from_rgb(46, 204, 113), #green status bar
    )
    embed.add_field(name="Status", value=f"**{rel_type}** - Ready to download", inline=False)
    embed.add_field(name="Affected", value=project_name or f"Curseforge Project #{project_id}", inline=False)

    if ts:
        embed.add_field(name="Updated", value=f"<t:{ts}:R>", inline=False)
        embed.set.footer(text=f"Released: <t:{ts}:f>")
    else:
        embed.add_field(name="Updated", value=f"<t:{ts}:R>", inline=False)
    
    embed.add_field(name="Game Versions", value="Recently", inline=False)
    embed.add_field(name="Release Type", value=rel_type, inline=True)
    return embed

#---------------DB helpers---------------

def _ensure_tables():
    db_init()
    with get_conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS cf_subs (
        project_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        mention TEXT,
        last_file_id INTEGER,
        PRIMARY KEY(project_id, guild_id)
        )
        """)

def add_or_update_sub(project_id: int, guild_id: int, channel_id: int, mention: Optional[str]):
    _ensure_tables()
    with get_conn() as c:
        c.execute("""
        INSERT INTO cf_subs(project_id, guild_id, channel_id, mention, last_file_id)
          VALUES(?,?,?,?,COALESCE((SELECT last_file_id FROM cf_subs WHERE project_id=? AND guild_id=?), NULL))
          ON CONFLICT(project_id, guild_id) DO UPDATE SET
            channel_id=excluded.channel_id,
            mention=excluded.mention
        """,(project_id, guild_id, channel_id, mention, project_id, guild_id))

    def remove_sub(project_id: int, guild_id: int) -> bool:
        _ensure_tables()
        with get_conn() as c:
            cur = c.execute("DELETE FROM cf_subs WHERE project_id=? AND guild_id=?", (project_id, guild_id))
            return cur.rowcount > 0
    
    def list_subs(guild_id: int):
        _ensure_tables
        with get_conn() as c:
            return [dict(r) for r in c.execute]