# D&D 5e AI Assistant — Web App

A self-hosted web app that gives your D&D group access to a rules-accurate AI assistant powered by Claude. Includes user auth, daily message limits, and an admin panel.

## Features

- **Rules lookups** — spells, class features, monsters, items, conditions
- **Character creation** — step-by-step guidance with accurate stat calculations
- **2024 & 2014 PHB** — always clarifies which edition applies
- **Source-cited answers** — looks up local 5etools data instead of guessing
- **User accounts** — register/login, admin activates accounts
- **Daily limits** — configurable per user to control API costs
- **Admin panel** — manage users, limits, and pending registrations

## Requirements

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)
- The 5etools data files (see below)

## Setup

```bash
git clone https://github.com/amaurer1010/dnd-web.git
cd dnd-web
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file:

```env
ANTHROPIC_API_KEY=your_api_key_here
SECRET_KEY=any_random_string
ADMIN_USERNAME=admin
ADMIN_PASSWORD=yourpassword
DATA_DIR=./data
PROMPT_PATH=./prompt.md
```

## Data Files

This app requires the 5etools JSON data files in a `data/` directory. Clone the local version to get them:

```bash
git clone https://github.com/amaurer1010/dnd-assistant.git
cp -r dnd-assistant/data ./data
```

## Run

```bash
python app.py
```

Visit http://localhost:5000

## Deploying on a Raspberry Pi with Cloudflare Tunnel

1. Copy files to the Pi and install dependencies
2. Install [cloudflared](https://github.com/cloudflare/cloudflared)
3. Run the app as a systemd service
4. Run `cloudflared tunnel --url http://localhost:5000` as a systemd service

See the [dnd-assistant repo](https://github.com/amaurer1010/dnd-assistant) for the self-hosted CLI version that doesn't require an API key.
