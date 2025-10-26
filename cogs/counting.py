<<<<<<< HEAD
# cogs/counting.py
from __future__ import annotations
import re, unicodedata
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from services.db import init, get_state, set_state, bump_user_count, top_counters, get_setting, set_setting

EXTREME_MAX_EXPONENT = 18
MILESTONES = {69, 420, 777, 1000, 1337}
NUM_TOKEN = re.compile(r"[-+]?((\d[\d,_\s]*)|(\d+(\.\d+)?(e[+-]?\d+)))$", re.IGNORECASE)

def get_extreme_mode(gid: int) -> bool:
    return (get_setting(gid, "extreme_mode", "false") == "true")

def _normalize_unicode_digits(s: str) -> str:
    return "".join(str(unicodedata.digit(ch)) if ch.isdigit() and not ('0' <= ch <= '9') else ch for ch in s)

def _try_parse_numeric_token(tok: str) -> Optional[int]:
    tok = _normalize_unicode_digits(tok.strip())
    if not tok or tok.startswith("+"):
        return None
    clean = tok.replace(",", "").replace("_", "").replace(" ", "")
    if any(c in clean.lower() for c in ("e", ".")):
        try:
            d = Decimal(clean)
            if d == d.to_integral_value():
                if "e" in clean.lower():
                    try:
                        exp = int(re.split(r"e", clean, flags=re.IGNORECASE)[1])
                        if abs(exp) > EXTREME_MAX_EXPONENT:
                            return None
                    except Exception:
                        return None
                return int(d)
            return None
        except InvalidOperation:
            return None
    if clean.lstrip("-").isdigit():
        try:
            return int(clean)
        except ValueError:
            return None
    return None

def _is_power10_milestone(n: int) -> bool:
    if n <= 0:
        return False
    s = str(n)
    return s[0] in "123456789" and set(s[1:]) == {"0"} and len(s) >= 5

def is_milestone(n: int) -> bool:
    return n in MILESTONES or _is_power10_milestone(n)

def parse_count_message(content: str, expected: int, extreme: bool) -> Optional[int]:
    text = content.strip()
    if not extreme:
        return _try_parse_numeric_token(text)
    val = _try_parse_numeric_token(text)
    if val is not None:
        return val
    tokens = [t for t in re.split(r"[^\w\-\+\.,\s]", text) if t]
    for chunk in " ".join(tokens).split():
        if len(chunk) > 32:
            continue
        cand = _try_parse_numeric_token(chunk)
        if cand is not None:
            return cand
    return None

async def backfill_from_history(channel: discord.TextChannel, extreme: bool, max_messages: int = 5000) -> Tuple[int, Optional[int]]:
    last_number = 0
    last_user: Optional[int] = None
    expected: Optional[int] = None
    async for msg in channel.history(limit=max_messages, oldest_first=False):
        if msg.author.bot:
            continue
        val = parse_count_message(msg.content, expected or 0, extreme)
        if val is None:
            continue
        if expected is None:
            last_number = val
            last_user = msg.author.id
            expected = val - 1
            continue
        if val == expected:
            expected -= 1
            continue
        break
    return max(0, last_number), last_user

class Counting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init()

    # --------- Commands ---------

    @app_commands.command(description="Set the counting channel (auto backfill)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel where counting happens")
    async def setcountingchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        await interaction.response.defer(ephemeral=True, thinking=True)
        _ = get_state(gid)  # ensure row exists
        extreme = get_extreme_mode(gid)
        last_num, last_user = await backfill_from_history(channel, extreme)
        set_state(gid, channel_id=channel.id, last_number=last_num, last_user_id=last_user)
        await interaction.followup.send(
            f"✅ Counting channel set to {channel.mention}. Detected last **{last_num}** → next **{(last_num or 0)+1}**.",
            ephemeral=True
        )

    @app_commands.command(description="Show counting stats")
    async def stats(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        st = get_state(gid)
        nextn = (st["last_number"] or 0) + 1
        high_user = f"<@{st['high_scorer_id']}>" if st.get("high_scorer_id") else "—"
        rows = top_counters(gid, 10)
        lb = "\n".join([f"{i+1}. <@{r['user_id']}> — {r['cnt']}" for i, r in enumerate(rows)]) or "(no data yet)"
        embed = discord.Embed(title="CelestiGuard Stats", color=discord.Color.blurple())
        embed.add_field(name="Counting Channel", value=(f"<#{st['channel_id']}>" if st.get("channel_id") else "*not set*"), inline=True)
        embed.add_field(name="Current Count", value=str(st["last_number"]), inline=True)
        embed.add_field(name="Next Number", value=str(nextn), inline=True)
        embed.add_field(name="High Score", value=str(st["high_score"]), inline=True)
        embed.add_field(name="Record Holder", value=high_user, inline=True)
        embed.add_field(name="Top Counters", value=lb, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Set current count (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setcount(self, interaction: discord.Interaction, value: int):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        value = max(0, value)
        set_state(gid, last_number=value, last_user_id=None)
        await interaction.response.send_message(
            f"🔧 Count set to **{value}**. Next **{(value or 0)+1}**.",
            ephemeral=True
        )

    @app_commands.command(description="Reset count to 0 (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resetcount(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        set_state(gid, last_number=0, last_user_id=None)
        await interaction.response.send_message("🧹 Count reset to **0**. Next **1**.", ephemeral=True)

    @app_commands.command(description="Toggle Extreme Mode")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def extrememode(self, interaction: discord.Interaction, value: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        set_setting(gid, "extreme_mode", "true" if value else "false")
        await interaction.response.send_message(
            "🧨 Extreme Mode ENABLED" if value else "⛔ Extreme Mode DISABLED",
            ephemeral=True
        )

    @app_commands.command(description="Toggle deletion of wrong messages in counting channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def countconfig(self, interaction: discord.Interaction, delete_wrong: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        set_setting(gid, "delete_wrong", "true" if delete_wrong else "false")
        await interaction.response.send_message(f"🧰 delete_wrong set to {delete_wrong}", ephemeral=True)

    @app_commands.command(description="Rescan history and sync current count (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def synccount(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        st = get_state(gid)
        cid = st.get("channel_id")
        if not cid:
            await interaction.response.send_message("Counting channel not set.", ephemeral=True)
            return

        ch = interaction.client.get_channel(cid)
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("Counting channel must be a text channel I can read.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        extreme = get_extreme_mode(gid)
        last_num, last_user = await backfill_from_history(ch, extreme)
        set_state(gid, last_number=last_num, last_user_id=last_user)
        await interaction.followup.send(f"🔄 Synced. Last **{last_num}** → next **{(last_num or 0)+1}**.", ephemeral=True)

    # --------- Listener ---------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # ignore DMs and bots
        if message.author.bot or message.guild is None:
            return

        gid = message.guild.id
        st = get_state(gid)
        ch_id = st.get("channel_id")
        if not ch_id or message.channel.id != ch_id:
            return

        extreme = get_extreme_mode(gid)
        delete_wrong = (get_setting(gid, "delete_wrong", "true") == "true")
        expected = (st["last_number"] or 0) + 1
        n = parse_count_message(message.content, expected, extreme)

        if n is None:
            if delete_wrong:
                try:
                    await message.delete()
                except Exception:
                    pass
            return

        reason = None
        same_user = (st.get("last_user_id") == message.author.id)
        if n != expected:
            reason = f"Expected **{expected}**."
        elif same_user and not (extreme and is_milestone(n)):
            reason = "You can't count twice in a row."

        if reason:
            if delete_wrong:
                try:
                    await message.delete()
                except Exception:
                    pass
            try:
                note = await message.channel.send(
                    f"❌ Wrong count by {message.author.mention}: {reason} Count resets to **0**. Next is **1**."
                )
                await note.delete(delay=6)
            except Exception:
                pass
            set_state(gid, last_number=0, last_user_id=None)
            return

        # Good number
        hs = st.get("high_score", 0)
        hi = st.get("high_scorer_id")
        if n > hs:
            hs = n
            hi = message.author.id
            try:
                await message.add_reaction("🏆")
            except Exception:
                pass
        else:
            try:
                await message.add_reaction("✅")
            except Exception:
                pass

        set_state(gid, last_number=n, last_user_id=message.author.id, high_score=hs, high_scorer_id=hi)
        bump_user_count(gid, message.author.id)

async def setup(bot: commands.Bot):
    await bot.add_cog(Counting(bot))
=======
# cogs/counting.py
from __future__ import annotations
import re, unicodedata
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from services.db import init, get_state, set_state, bump_user_count, top_counters, get_setting, set_setting

EXTREME_MAX_EXPONENT = 18
MILESTONES = {69, 420, 777, 1000, 1337}
NUM_TOKEN = re.compile(r"[-+]?((\d[\d,_\s]*)|(\d+(\.\d+)?(e[+-]?\d+)))$", re.IGNORECASE)

def get_extreme_mode(gid: int) -> bool:
    return (get_setting(gid, "extreme_mode", "false") == "true")

def _normalize_unicode_digits(s: str) -> str:
    return "".join(str(unicodedata.digit(ch)) if ch.isdigit() and not ('0' <= ch <= '9') else ch for ch in s)

def _try_parse_numeric_token(tok: str) -> Optional[int]:
    tok = _normalize_unicode_digits(tok.strip())
    if not tok or tok.startswith("+"):
        return None
    clean = tok.replace(",", "").replace("_", "").replace(" ", "")
    if any(c in clean.lower() for c in ("e", ".")):
        try:
            d = Decimal(clean)
            if d == d.to_integral_value():
                if "e" in clean.lower():
                    try:
                        exp = int(re.split(r"e", clean, flags=re.IGNORECASE)[1])
                        if abs(exp) > EXTREME_MAX_EXPONENT:
                            return None
                    except Exception:
                        return None
                return int(d)
            return None
        except InvalidOperation:
            return None
    if clean.lstrip("-").isdigit():
        try:
            return int(clean)
        except ValueError:
            return None
    return None

def _is_power10_milestone(n: int) -> bool:
    if n <= 0:
        return False
    s = str(n)
    return s[0] in "123456789" and set(s[1:]) == {"0"} and len(s) >= 5

def is_milestone(n: int) -> bool:
    return n in MILESTONES or _is_power10_milestone(n)

def parse_count_message(content: str, expected: int, extreme: bool) -> Optional[int]:
    text = content.strip()
    if not extreme:
        return _try_parse_numeric_token(text)
    val = _try_parse_numeric_token(text)
    if val is not None:
        return val
    tokens = [t for t in re.split(r"[^\w\-\+\.,\s]", text) if t]
    for chunk in " ".join(tokens).split():
        if len(chunk) > 32:
            continue
        cand = _try_parse_numeric_token(chunk)
        if cand is not None:
            return cand
    return None

async def backfill_from_history(channel: discord.TextChannel, extreme: bool, max_messages: int = 5000) -> Tuple[int, Optional[int]]:
    last_number = 0
    last_user: Optional[int] = None
    expected: Optional[int] = None
    async for msg in channel.history(limit=max_messages, oldest_first=False):
        if msg.author.bot:
            continue
        val = parse_count_message(msg.content, expected or 0, extreme)
        if val is None:
            continue
        if expected is None:
            last_number = val
            last_user = msg.author.id
            expected = val - 1
            continue
        if val == expected:
            expected -= 1
            continue
        break
    return max(0, last_number), last_user

class Counting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init()

    # --------- Commands ---------

    @app_commands.command(description="Set the counting channel (auto backfill)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel where counting happens")
    async def setcountingchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        await interaction.response.defer(ephemeral=True, thinking=True)
        _ = get_state(gid)  # ensure row exists
        extreme = get_extreme_mode(gid)
        last_num, last_user = await backfill_from_history(channel, extreme)
        set_state(gid, channel_id=channel.id, last_number=last_num, last_user_id=last_user)
        await interaction.followup.send(
            f"✅ Counting channel set to {channel.mention}. Detected last **{last_num}** → next **{(last_num or 0)+1}**.",
            ephemeral=True
        )

    @app_commands.command(description="Show counting stats")
    async def stats(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        st = get_state(gid)
        nextn = (st["last_number"] or 0) + 1
        high_user = f"<@{st['high_scorer_id']}>" if st.get("high_scorer_id") else "—"
        rows = top_counters(gid, 10)
        lb = "\n".join([f"{i+1}. <@{r['user_id']}> — {r['cnt']}" for i, r in enumerate(rows)]) or "(no data yet)"
        embed = discord.Embed(title="CelestiGuard Stats", color=discord.Color.blurple())
        embed.add_field(name="Counting Channel", value=(f"<#{st['channel_id']}>" if st.get("channel_id") else "*not set*"), inline=True)
        embed.add_field(name="Current Count", value=str(st["last_number"]), inline=True)
        embed.add_field(name="Next Number", value=str(nextn), inline=True)
        embed.add_field(name="High Score", value=str(st["high_score"]), inline=True)
        embed.add_field(name="Record Holder", value=high_user, inline=True)
        embed.add_field(name="Top Counters", value=lb, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Set current count (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setcount(self, interaction: discord.Interaction, value: int):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        value = max(0, value)
        set_state(gid, last_number=value, last_user_id=None)
        await interaction.response.send_message(
            f"🔧 Count set to **{value}**. Next **{(value or 0)+1}**.",
            ephemeral=True
        )

    @app_commands.command(description="Reset count to 0 (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resetcount(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        set_state(gid, last_number=0, last_user_id=None)
        await interaction.response.send_message("🧹 Count reset to **0**. Next **1**.", ephemeral=True)

    @app_commands.command(description="Toggle Extreme Mode")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def extrememode(self, interaction: discord.Interaction, value: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        set_setting(gid, "extreme_mode", "true" if value else "false")
        await interaction.response.send_message(
            "🧨 Extreme Mode ENABLED" if value else "⛔ Extreme Mode DISABLED",
            ephemeral=True
        )

    @app_commands.command(description="Toggle deletion of wrong messages in counting channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def countconfig(self, interaction: discord.Interaction, delete_wrong: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        set_setting(gid, "delete_wrong", "true" if delete_wrong else "false")
        await interaction.response.send_message(f"🧰 delete_wrong set to {delete_wrong}", ephemeral=True)

    @app_commands.command(description="Rescan history and sync current count (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def synccount(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        gid = interaction.guild.id

        st = get_state(gid)
        cid = st.get("channel_id")
        if not cid:
            await interaction.response.send_message("Counting channel not set.", ephemeral=True)
            return

        ch = interaction.client.get_channel(cid)
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("Counting channel must be a text channel I can read.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        extreme = get_extreme_mode(gid)
        last_num, last_user = await backfill_from_history(ch, extreme)
        set_state(gid, last_number=last_num, last_user_id=last_user)
        await interaction.followup.send(f"🔄 Synced. Last **{last_num}** → next **{(last_num or 0)+1}**.", ephemeral=True)

    # --------- Listener ---------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # ignore DMs and bots
        if message.author.bot or message.guild is None:
            return

        gid = message.guild.id
        st = get_state(gid)
        ch_id = st.get("channel_id")
        if not ch_id or message.channel.id != ch_id:
            return

        extreme = get_extreme_mode(gid)
        delete_wrong = (get_setting(gid, "delete_wrong", "true") == "true")
        expected = (st["last_number"] or 0) + 1
        n = parse_count_message(message.content, expected, extreme)

        if n is None:
            if delete_wrong:
                try:
                    await message.delete()
                except Exception:
                    pass
            return

        reason = None
        same_user = (st.get("last_user_id") == message.author.id)
        if n != expected:
            reason = f"Expected **{expected}**."
        elif same_user and not (extreme and is_milestone(n)):
            reason = "You can't count twice in a row."

        if reason:
            if delete_wrong:
                try:
                    await message.delete()
                except Exception:
                    pass
            try:
                note = await message.channel.send(
                    f"❌ Wrong count by {message.author.mention}: {reason} Count resets to **0**. Next is **1**."
                )
                await note.delete(delay=6)
            except Exception:
                pass
            set_state(gid, last_number=0, last_user_id=None)
            return

        # Good number
        hs = st.get("high_score", 0)
        hi = st.get("high_scorer_id")
        if n > hs:
            hs = n
            hi = message.author.id
            try:
                await message.add_reaction("🏆")
            except Exception:
                pass
        else:
            try:
                await message.add_reaction("✅")
            except Exception:
                pass

        set_state(gid, last_number=n, last_user_id=message.author.id, high_score=hs, high_scorer_id=hi)
        bump_user_count(gid, message.author.id)

async def setup(bot: commands.Bot):
    await bot.add_cog(Counting(bot))
>>>>>>> dd54628 (start project)
