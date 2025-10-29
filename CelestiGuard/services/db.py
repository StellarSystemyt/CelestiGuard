import os
import time
import sqlite3
from contextlib import closing
from typing import Optional

# --- DB file under ./data ---
os.makedirs("data", exist_ok=True)
DB_PATH = os.path.join("data", "celestiguard.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

# --- Schema ---
SCHEMA = """

CREATE TABLE IF NOT EXISTS moderation_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  moderator_id INTEGER NOT NULL,
  action TEXT NOT NULL,                -- 'purge','timeout','kick','ban','unban'
  reason TEXT,
  created_ts INTEGER NOT NULL,
  extra_json TEXT                      -- optional metadata (e.g., timeout until, count purged)
);


-- Key/Value settings
CREATE TABLE IF NOT EXISTS guild_settings (
  guild_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (guild_id, key)
);

-- Counting state
CREATE TABLE IF NOT EXISTS counting_state (
  guild_id INTEGER PRIMARY KEY,
  channel_id INTEGER,
  last_number INTEGER NOT NULL DEFAULT 0,
  last_user_id INTEGER,
  high_score INTEGER NOT NULL DEFAULT 0,
  high_scorer_id INTEGER
);

-- Per-user counting tallies
CREATE TABLE IF NOT EXISTS counting_user_counts (
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  cnt INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
);

-- One-time dashboard tokens
CREATE TABLE IF NOT EXISTS ephemeral_tokens (
  token TEXT PRIMARY KEY,
  guild_id INTEGER,
  expires_ts INTEGER NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  created_ts INTEGER NOT NULL
);

-- General server-management config (used by dashboard)
CREATE TABLE IF NOT EXISTS guild_config (
  guild_id INTEGER PRIMARY KEY,
  log_channel_id INTEGER,
  welcome_channel_id INTEGER,
  welcome_message TEXT,
  autorole_id INTEGER
);

-- (Optional) moderation log (used by moderation/admin cogs)
CREATE TABLE IF NOT EXISTS mod_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  moderator_id INTEGER NOT NULL,
  action TEXT NOT NULL,     -- kick/ban/timeout/untimeout/warn/purge/nick/lock/unlock/slowmode/clearwarnings/unban
  reason TEXT,
  points INTEGER DEFAULT 0, -- for warn
  created_ts INTEGER NOT NULL
);

-- (Optional) warnings table (used by moderation cogs)
CREATE TABLE IF NOT EXISTS warnings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  moderator_id INTEGER NOT NULL,
  points INTEGER NOT NULL DEFAULT 1,
  reason TEXT,
  created_ts INTEGER NOT NULL
);
"""

def init():
    with closing(get_conn()) as c:
        c.executescript(SCHEMA)

# ---------------- Settings (key/value) ----------------
def get_setting(guild_id: int, key: str, default: Optional[str] = None) -> Optional[str]:
    with get_conn() as c:
        row = c.execute(
            "SELECT value FROM guild_settings WHERE guild_id=? AND key=?",
            (guild_id, key)
        ).fetchone()
        return row["value"] if row else default

def set_setting(guild_id: int, key: str, value: Optional[str]) -> None:
    with get_conn() as c:
        if value is None:
            c.execute("DELETE FROM guild_settings WHERE guild_id=? AND key=?", (guild_id, key))
        else:
            c.execute(
                "INSERT INTO guild_settings(guild_id, key, value) VALUES(?,?,?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                (guild_id, key, value),
            )

# ---------------- Counting state ----------------
def get_state(guild_id: int) -> dict:
    with get_conn() as c:
        row = c.execute("SELECT * FROM counting_state WHERE guild_id=?", (guild_id,)).fetchone()
        if row:
            return dict(row)
        c.execute(
            "INSERT INTO counting_state(guild_id, channel_id, last_number, last_user_id, high_score, high_scorer_id) "
            "VALUES (?,?,?,?,?,?)",
            (guild_id, None, 0, None, 0, None),
        )
        return {
            "guild_id": guild_id,
            "channel_id": None,
            "last_number": 0,
            "last_user_id": None,
            "high_score": 0,
            "high_scorer_id": None,
        }

def set_state(guild_id: int, **kwargs) -> None:
    if not kwargs:
        return
    keys = ", ".join([f"{k}=?" for k in kwargs.keys()])
    vals = list(kwargs.values()) + [guild_id]
    with get_conn() as c:
        c.execute(f"UPDATE counting_state SET {keys} WHERE guild_id=?", vals)

def bump_user_count(guild_id: int, user_id: int) -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO counting_user_counts(guild_id, user_id, cnt) VALUES (?,?,0) "
            "ON CONFLICT(guild_id, user_id) DO NOTHING",
            (guild_id, user_id),
        )
        c.execute(
            "UPDATE counting_user_counts SET cnt=cnt+1 WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        )

def top_counters(guild_id: int, limit: int = 10):
    with get_conn() as c:
        return c.execute(
            "SELECT user_id, cnt FROM counting_user_counts WHERE guild_id=? ORDER BY cnt DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()

# ---------------- Guild Config (dashboard server management) ----------------
def set_guild_config(guild_id: int, **fields) -> None:
    """Upsert selected fields for a guild's server-management settings."""
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields.keys())
    vals = list(fields.values())
    with get_conn() as c:
        # ensure row exists
        c.execute("INSERT OR IGNORE INTO guild_config(guild_id) VALUES (?)", (guild_id,))
        c.execute(f"UPDATE guild_config SET {cols} WHERE guild_id=?", (*vals, guild_id))

def get_guild_config(guild_id: int) -> dict:
    """Fetch server-management settings for a guild. Returns sensible defaults."""
    with get_conn() as c:
        row = c.execute(
            "SELECT log_channel_id, welcome_channel_id, welcome_message, autorole_id "
            "FROM guild_config WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
    if not row:
        return {
            "guild_id": guild_id,
            "log_channel_id": None,
            "welcome_channel_id": None,
            "welcome_message": None,
            "autorole_id": None,
        }
    return {
        "guild_id": guild_id,
        "log_channel_id": row[0],
        "welcome_channel_id": row[1],
        "welcome_message": row[2],
        "autorole_id": row[3],
    }




# --- add near the end of services/db.py ---

import json, time

def add_case(guild_id: int, user_id: int, moderator_id: int, action: str,
             reason: str | None = None, extra: dict | None = None) -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO moderation_cases(guild_id,user_id,moderator_id,action,reason,created_ts,extra_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (guild_id, user_id, moderator_id, action, reason or "", int(time.time()),
             json.dumps(extra or {}))
        )
        return cur.lastrowid

def list_cases(guild_id: int, limit: int = 25):
    with get_conn() as c:
        return c.execute(
            "SELECT * FROM moderation_cases WHERE guild_id=? ORDER BY id DESC LIMIT ?",
            (guild_id, limit)
        ).fetchall()

def get_case(guild_id: int, case_id: int):
    with get_conn() as c:
        return c.execute(
            "SELECT * FROM moderation_cases WHERE guild_id=? AND id=?",
            (guild_id, case_id)
        ).fetchone()

# ---------------- Moderation helpers (optional but handy) ----------------
def add_mod_action(guild_id: int, user_id: int, moderator_id: int, action: str,
                   reason: Optional[str] = None, points: int = 0, ts: Optional[int] = None) -> None:
    ts = ts or int(time.time())
    with get_conn() as c:
        c.execute(
            "INSERT INTO mod_actions(guild_id,user_id,moderator_id,action,reason,points,created_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (guild_id, user_id, moderator_id, action, reason, points, ts),
        )

def add_warning(guild_id: int, user_id: int, moderator_id: int,
                points: int = 1, reason: Optional[str] = None, ts: Optional[int] = None) -> None:
    ts = ts or int(time.time())
    with get_conn() as c:
        c.execute(
            "INSERT INTO warnings(guild_id,user_id,moderator_id,points,reason,created_ts) "
            "VALUES (?,?,?,?,?,?)",
            (guild_id, user_id, moderator_id, points, reason, ts),
        )

def get_warnings(guild_id: int, user_id: int):
    with get_conn() as c:
        return c.execute(
            "SELECT id, points, reason, created_ts, moderator_id "
            "FROM warnings WHERE guild_id=? AND user_id=? ORDER BY created_ts DESC",
            (guild_id, user_id),
        ).fetchall()

def clear_warnings(guild_id: int, user_id: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (guild_id, user_id))
