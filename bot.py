# bot.py
from __future__ import annotations
import os
import sys
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

import discord
from discord.ext import commands

# ── Load env FIRST ────────────────────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5500"))
CELESTIGUARD_VERSION = os.getenv("CELESTIGUARD_VERSION", "1.0.0")

# Privileged intents toggles (so you can deploy even if not enabled yet)
INTENTS_MEMBERS = os.getenv("INTENTS_MEMBERS", "false").lower() in ("1", "true", "yes", "on")
INTENTS_PRESENCES = os.getenv("INTENTS_PRESENCES", "false").lower() in ("1", "true", "yes", "on")

# Optional: comma, space or newline separated list of cogs to load
COGS_RAW = os.getenv("COGS", "cogs.counting,cogs.logs,cogs.moderation,cogs.admin")
COGS = [c.strip() for chunk in COGS_RAW.split("\n") for c in chunk.split(",") if c.strip()]

# Import webapp after env is loaded
from services.webapp import create_app, set_bot, set_brand_avatar  # noqa: E402
import uvicorn  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("celestiguard")
log.setLevel(logging.INFO)

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# console
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_formatter)
log.addHandler(_ch)

# rotating file (~/CelestiGuard/logs/celestiguard.log or ./logs/celestiguard.log)
logs_dir = os.path.join(os.getcwd(), "logs")
os.makedirs(logs_dir, exist_ok=True)
_fh = RotatingFileHandler(os.path.join(logs_dir, "celestiguard.log"),
                          maxBytes=2_000_000, backupCount=5, encoding="utf-8")
_fh.setFormatter(_formatter)
log.addHandler(_fh)

discord.utils.setup_logging(level=logging.INFO, root=False)

if not DISCORD_TOKEN:
    raise SystemExit("❌ Set DISCORD_TOKEN in your .env file!")

# ── Intents ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True     # needed to read count messages
intents.guilds = True
intents.messages = True
# Toggle privileged ones via env
intents.members = INTENTS_MEMBERS
intents.presences = INTENTS_PRESENCES

# ── Bot ───────────────────────────────────────────────────────────────────────
bot = commands.Bot(command_prefix="!", intents=intents)

async def _load_cogs_safely() -> None:
    for ext in COGS:
        try:
            await bot.load_extension(ext)
            log.info("Loaded cog: %s", ext)
        except Exception as e:
            log.error("Failed to load cog %s: %s", ext, e)

@bot.event
async def on_ready():
    assert bot.user is not None  # quiet type checker
    await bot.tree.sync()
    try:
        await bot.change_presence(activity=discord.Game(name="counting | /setcountingchannel"))
    except Exception:
        pass

    # Pick a nice brand avatar for the dashboard
    avatar_url = str(bot.user.display_avatar.with_size(64).url)
    try:
        if bot.user.avatar is None:
            # (Optional) prefer the application icon if bot has default avatar
            app_info = await bot.application_info()
            if app_info and app_info.icon:
                avatar_url = str(app_info.icon.with_size(64).url)
    except Exception:
        pass
    set_brand_avatar(avatar_url)

    log.info("✅ CelestiGuard online as %s (%s) | v%s", bot.user, bot.user.id, CELESTIGUARD_VERSION)

async def main():
    # Start FastAPI dashboard alongside the bot
    set_bot(bot)
    app = create_app(version=CELESTIGUARD_VERSION)

    # Run uvicorn inside this process on localhost (nginx reverse proxies to it)
    config = uvicorn.Config(app=app, host=DASHBOARD_HOST, port=DASHBOARD_PORT, log_level="info", lifespan="on")
    server = uvicorn.Server(config)

    # Launch dashboard
    dash_task = asyncio.create_task(server.serve())

    # Launch bot
    async with bot:
        await _load_cogs_safely()
        try:
            await bot.start(DISCORD_TOKEN)
        finally:
            # ensure the dashboard stops if the bot stops
            if not dash_task.done():
                server.should_exit = True
                try:
                    await dash_task
                except Exception:
                    pass

if __name__ == "__main__":
    asyncio.run(main())