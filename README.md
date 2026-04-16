# fitness-bot 🤖💪

A personal AI fitness coach that lives in Discord. Pulls WHOOP recovery data and Strava activities, lets you log lifts via chat, and sends daily briefs + weekly summaries. Powered by Claude.

## What it does

- **Daily morning brief** — recovery score, HRV, sleep quality, and recommended training intent for the day
- **Weekly summary** — trends across running, lifting, and recovery
- **Conversational coach** — ask anything: "should I run hard today?", "how has my bench progressed?", "why do I feel tired despite a green recovery?"
- **Lift logging via chat** — just message "bench 3x10 at 145" and it logs it, tracks progression, and flags PRs
- **Notion training journal** — auto-written in the background, no manual entry needed

## Stack

- Python 3.11+
- [discord.py](https://discordpy.readthedocs.io/) — Discord bot
- [WHOOP API](https://developer.whoop.com/) — recovery, sleep, HRV, strain
- [Strava API](https://developers.strava.com/) — activities
- [Anthropic Claude](https://docs.anthropic.com/) — AI reasoning layer
- [Notion API](https://developers.notion.com/) — training log output
- SQLite (via aiosqlite) — local lift log + notes storage

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/dylanglatt/fitness-bot.git
cd fitness-bot
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in all values in `.env`. See the sections below for how to get each credential.

### 3. Run

```bash
python main.py
```

---

## Getting your API credentials

### Discord
1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Create a new application → Bot → copy the token
3. Enable **Message Content Intent** under Privileged Gateway Intents
4. Invite the bot to your server with these permissions: Send Messages, Read Message History
5. Right-click your own Discord username → Copy User ID (enable Developer Mode in settings first)
6. Copy the channel IDs for your daily brief and training channels

### Strava
1. Go to [strava.com/settings/api](https://www.strava.com/settings/api) → create an app
2. Copy Client ID and Client Secret
3. To get your refresh token, run the OAuth flow once (see `docs/strava_auth.md`)

### WHOOP
1. Go to [developer.whoop.com](https://developer.whoop.com) → create an app
2. Copy Client ID and Client Secret
3. Run the OAuth flow once to get your refresh token (see `docs/whoop_auth.md`)

### Anthropic
1. Go to [console.anthropic.com](https://console.anthropic.com) → API Keys → create one

### Notion
1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → create an integration
2. Copy the Internal Integration Token
3. Create a database in Notion with these properties:
   - Date (Date), Recovery Score (Number), HRV (Number), RHR (Number),
     Sleep hrs (Number), Sleep Efficiency (Number), Activities (Text),
     Lifts (Text), Notes (Text), Daily Brief (Text)
4. Share the database with your integration, copy the database ID from the URL

---

## Project structure

```
fitness-bot/
├── main.py               # Entry point
├── config.py             # All config / env vars
├── requirements.txt
├── .env.example
├── bot/
│   ├── discord_bot.py    # Bot setup, message routing
│   └── scheduler.py      # Daily brief + weekly summary triggers
├── integrations/
│   ├── strava.py         # Strava API client
│   ├── whoop.py          # WHOOP API client
│   └── notion.py         # Notion write client
├── ai/
│   ├── coach.py          # Orchestrates data + Claude calls
│   └── prompts.py        # System prompt + fitness knowledge
├── data/
│   └── database.py       # SQLite for lifts + notes
└── docs/
    ├── strava_auth.md    # Strava OAuth walkthrough
    └── whoop_auth.md     # WHOOP OAuth walkthrough
```

---

## Deployment (self-hosted)

A $6/month [Hetzner](https://hetzner.com) or [DigitalOcean](https://digitalocean.com) VPS running Ubuntu is plenty.

```bash
# Install Python, clone repo, set up .env, then run as a service:
sudo nano /etc/systemd/system/fitness-bot.service
```

```ini
[Unit]
Description=fitness-bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/fitness-bot
ExecStart=/home/ubuntu/fitness-bot/venv/bin/python main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable fitness-bot
sudo systemctl start fitness-bot
```
