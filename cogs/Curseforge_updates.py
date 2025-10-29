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