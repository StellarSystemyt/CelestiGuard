# cogs/logs.py
from __future__ import annotations
from typing import Iterable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.db import get_setting, set_setting

# ---------- helpers: safe channel formatting / sending ----------

from typing import Optional, Union, TypeAlias

# Only these support .send()
TextSendable: TypeAlias = Union[
    discord.TextChannel,
    discord.Thread,
    discord.DMChannel,
    discord.GroupChannel,
]

def format_channel_ref(ch: discord.abc.GuildChannel | discord.abc.PrivateChannel) -> str:
    """Return a safe, readable channel reference for logs (no .mention on DM/Group)."""
    if isinstance(ch, discord.TextChannel):
        return ch.mention
    if isinstance(ch, discord.Thread):
        base = f"#{ch.name}" if getattr(ch, "name", None) else f"Thread:{ch.id}"
        return f"{base} (thread)"
    if isinstance(ch, discord.ForumChannel):
        return f"{ch.name} (forum)"
    if isinstance(ch, discord.CategoryChannel):
        return f"{ch.name} (category)"
    if isinstance(ch, discord.DMChannel):
        u = ch.recipient
        uname = f"{u} ({u.id})" if u else "unknown user"
        return f"DM with {uname}"
    if isinstance(ch, discord.GroupChannel):
        return f"Group DM '{ch.name or 'unnamed'}' ({len(ch.recipients)} members)"
    return f"{type(ch).__name__} {getattr(ch, 'id', '?')}"

async def try_send(dest: TextSendable, *, embed: Optional[discord.Embed] = None, content: Optional[str] = None):
    """
    Send only if destination supports .send(). Build kwargs so Pylance sees correct overload.
    """
    try:
        if embed is not None and content is not None:
            await dest.send(content=content, embed=embed)
        elif embed is not None:
            await dest.send(embed=embed)
        elif content is not None:
            await dest.send(content=content)
        # if both None, do nothing
    except Exception:
        pass

def get_log_channel(guild: discord.Guild) -> Optional[TextSendable]:
    """
    Fetch the configured log channel from DB (key: 'log_channel_id').
    Return ONLY a sendable channel type (TextChannel/Thread/DM/Group), else None.
    """
    cid_str = get_setting(guild.id, "log_channel_id", None)
    if not cid_str:
        return None
    try:
        cid = int(cid_str)
    except ValueError:
        return None

    ch = guild.get_channel(cid)
    if isinstance(ch, (discord.TextChannel, discord.Thread)):
        return ch

    # Not a Guild text/thread; never DM/Group from guild lookup.
    # If you ever store a thread id and it isn't cached, you could fetch here:
    # try:
    #     fetched = await guild.fetch_channel(cid)  # would need to make this async
    #     if isinstance(fetched, (discord.TextChannel, discord.Thread)):
    #         return fetched
    # except Exception:
    #     pass

    return None


# ---------- Cog ----------

class Logs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Slash command to set the log channel
    @app_commands.command(description="Set the channel where CelestiGuard will post moderation logs")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Text channel (or thread) for logs")
    async def setlogchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        set_setting(interaction.guild.id, "log_channel_id", str(channel.id))
        await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

    # Role changes: on_member_update
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Guard: sometimes fires for DMs or missing guild (shouldn’t, but be safe)
        guild = after.guild if hasattr(after, "guild") else None
        if guild is None:
            return

        # Compare roles
        before_set = set(before.roles)
        after_set = set(after.roles)
        added = [r for r in (after_set - before_set) if r.name != "@everyone"]
        removed = [r for r in (before_set - after_set) if r.name != "@everyone"]

        if not added and not removed:
            return

        dest = get_log_channel(guild)
        if dest is None:
            return  # not configured

        emb = discord.Embed(color=discord.Color.blurple(), title="Role Update")
        emb.set_author(name=str(after), icon_url=after.display_avatar.url)
        emb.add_field(name="User", value=f"{after.mention} (`{after.id}`)", inline=False)

        if added:
            emb.add_field(
                name="Roles Added",
                value=", ".join(r.mention for r in added),
                inline=False,
            )
        if removed:
            emb.add_field(
                name="Roles Removed",
                value=", ".join(r.mention for r in removed),
                inline=False,
            )

        await try_send(dest, embed=emb)

    # Optional: basic channel create/delete rename logs (safe formatting)
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        dest = get_log_channel(guild)
        if dest is None:
            return
        emb = discord.Embed(color=discord.Color.green(), title="Channel Created")
        emb.add_field(name="Channel", value=format_channel_ref(channel), inline=False)
        await try_send(dest, embed=emb)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        dest = get_log_channel(guild)
        if dest is None:
            return
        emb = discord.Embed(color=discord.Color.red(), title="Channel Deleted")
        emb.add_field(name="Channel", value=format_channel_ref(channel), inline=False)
        await try_send(dest, embed=emb)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        guild = getattr(after, "guild", None)
        if guild is None:
            return
        dest = get_log_channel(guild)
        if dest is None:
            return

        # Only emit simple name/category changes to avoid noise
        name_changed = getattr(before, "name", None) != getattr(after, "name", None)
        if not name_changed:
            return

        emb = discord.Embed(color=discord.Color.orange(), title="Channel Updated")
        emb.add_field(name="Before", value=format_channel_ref(before), inline=False)
        emb.add_field(name="After", value=format_channel_ref(after), inline=False)
        await try_send(dest, embed=emb)

async def setup(bot: commands.Bot):
    await bot.add_cog(Logs(bot))
