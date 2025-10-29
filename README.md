
# CelestiGuard

Discord counting bot + FastAPI dashboard.

## Setup
1) Install Python 3.12+ (you have 3.13, good).
2) `cd CelestiGuard`
3) `python -m pip install -U -r requirements.txt`
4) Copy `.env.example` → `.env` and fill in your tokens.

## Run
- `python bot.py`
- Dashboard: http://127.0.0.1:8000/?token=YOUR_DASHBOARD_TOKEN

## First steps in Discord
- Invite the bot with scopes: `bot` + `applications.commands`
- Permissions: View Channels, Send Messages, Read Message History (optional: Manage Messages)
- Run `/setcountingchannel` in your server and pick the channel.

## Security
- Keep your **DISCORD_TOKEN** and **DASHBOARD_TOKEN** private.
- Use the dashboard's one-time share links if you need to let someone view a guild page.
