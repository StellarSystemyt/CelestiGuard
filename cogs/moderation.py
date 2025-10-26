from __future__ import annotations

from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# These helpers are assumed to exist in services.db in your project
# (they showed up in your Pylance hints). They must accept `int` guild IDs.
from services.db import add_case, list_cases  # type: ignore[reportMissingImports]


def _ensure_guild(inter: discord.Interaction) -> Tuple[discord.Guild, int]:
    """
    Make sure this command is used in a guild and return (guild, guild_id).
    Raises an app_commands.CheckFailure if used in DMs.
    """
    if inter.guild is None or inter.guild_id is None:
        # This makes Pylance happy and prevents calling guild methods in DMs.
        raise app_commands.CheckFailure("This command can only be used in a server (not in DMs).")
    return inter.guild, int(inter.guild_id)


class Moderation(commands.Cog):
    """Basic moderation commands (ban / unban / cases list)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- /ban ---
    @app_commands.command(description="Ban a member (requires Ban Members).")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.describe(
        member="Member to ban",
        reason="Reason shown in audit logs (optional)",
        delete_message_seconds="Delete previous messages (0-604800 seconds, optional)",
    )
    async def ban(
        self,
        inter: discord.Interaction,
        member: discord.Member,
        reason: Optional[str] = None,
        delete_message_seconds: Optional[int] = None,
    ):
        guild, gid = _ensure_guild(inter)

        # Clamp delete_message_seconds to allowed API range (0..604800)
        dms = 0
        if isinstance(delete_message_seconds, int):
            dms = max(0, min(604800, delete_message_seconds))

        await inter.response.defer(ephemeral=True, thinking=True)

        # Perform the ban
        try:
            await guild.ban(member, reason=reason or discord.utils.MISSING, delete_message_seconds=dms)
        except discord.Forbidden:
            await inter.followup.send("I don't have permission to ban that member.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await inter.followup.send(f"Discord error while banning: {e}", ephemeral=True)
            return

        # Log the case in DB with a concrete int guild_id
        try:
            add_case(
                guild_id=gid,
                action="BAN",
                target_id=int(member.id),
                moderator_id=int(inter.user.id),
                reason=reason or "",
            )
        except Exception:
            # Non-fatal: still consider command successful
            pass

        await inter.followup.send(f"🔨 Banned **{member}**. {'Reason: ' + reason if reason else ''}", ephemeral=True)

    # --- /unban ---
    @app_commands.command(description="Unban a user by ID or mention (requires Ban Members).")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.describe(user_id="The ID of the user to unban", reason="Reason (optional)")
    async def unban(self, inter: discord.Interaction, user_id: int, reason: Optional[str] = None):
        guild, gid = _ensure_guild(inter)

        await inter.response.defer(ephemeral=True, thinking=True)

        # Fetch a discord.User object for unban
        try:
            user = await self.bot.fetch_user(int(user_id))
        except discord.NotFound:
            await inter.followup.send("User not found.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await inter.followup.send(f"Discord error while fetching user: {e}", ephemeral=True)
            return

        try:
            await guild.unban(user, reason=reason or discord.utils.MISSING)
        except discord.NotFound:
            await inter.followup.send("That user is not currently banned.", ephemeral=True)
            return
        except discord.Forbidden:
            await inter.followup.send("I don't have permission to unban that user.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await inter.followup.send(f"Discord error while unbanning: {e}", ephemeral=True)
            return

        # Log the case
        try:
            add_case(
                guild_id=gid,
                action="UNBAN",
                target_id=int(user.id),
                moderator_id=int(inter.user.id),
                reason=reason or "",
            )
        except Exception:
            pass

        await inter.followup.send(f"✅ Unbanned **{user}**.", ephemeral=True)

    # --- /cases ---
    @app_commands.command(description="List recent moderation cases.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(limit="How many cases to show (default 10)")
    async def cases(self, inter: discord.Interaction, limit: Optional[int] = 10):
        _, gid = _ensure_guild(inter)
        lim = 10 if limit is None else max(1, min(50, int(limit)))  # keep it sane

        await inter.response.defer(ephemeral=True, thinking=True)

        try:
            rows = list_cases(guild_id=gid, limit=lim)
        except Exception as e:
            await inter.followup.send(f"DB error while fetching cases: {e}", ephemeral=True)
            return

        if not rows:
            await inter.followup.send("No cases recorded yet.", ephemeral=True)
            return

        embed = discord.Embed(title=f"Last {len(rows)} cases", color=discord.Color.blurple())
        for r in rows:
            action = r.get("action", "?")
            target = r.get("target_id")
            mod = r.get("moderator_id")
            rsn = r.get("reason") or ""
            embed.add_field(
                name=f"{action}",
                value=f"Target: <@{target}> (`{target}`)\nBy: <@{mod}> (`{mod}`)\n{('Reason: ' + rsn) if rsn else ''}",
                inline=False,
            )

        await inter.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
