# 🍎 Fruit Pass — Telegram Multiplayer Card Game Bot

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram%20Bot%20API-v21%2B-blue)](https://python-telegram-bot.org/)
[![Database](https://img.shields.io/badge/database-SQLite%20(aiosqlite)-green.svg)](https://sqlite.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> **Pass smart. Match four. Win the round.**  
> Real-time, server-authoritative multiplayer card-passing game designed for Telegram group chats (4 to 8 players).

---

## 🎮 How the Game Works

1. **Setup**: The host creates a room (`/newgame`) in any group chat. 4 to 8 players join (`/join`).
2. **Dealt Hands**: Every player is secretly dealt 4 fruit cards from a deck matched to the player count (e.g. 4 players = 4 sets of 4 fruits).
3. **Turn Flow**:
   - The active player receives a **Turn Panel** with a **"Make your choice!"** button.
   - Tapping the button opens Telegram Inline Query to pick a card from their secret hand.
   - When chosen, the bot announces: `🎴 [Player A] passed a card to [Player B]` and immediately triggers the next player's Turn Panel.
4. **Auto-Pass**: Players have 60 seconds per turn. If inactive, an auto-pass triggers automatically.
5. **Winning a Round**: When you pass a card and your 4 cards are all the same fruit, you win the round automatically!
6. **Winning the Match**: Play across 5, 8, or 10 rounds. The player with the highest total points wins the match!

---

## 🚀 Quick Start (Local Setup)

### 1. Prerequisites
- Python 3.10 or higher
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### 2. Clone & Install
```bash
git clone https://github.com/your-username/fruit-pass-bot.git
cd fruit-pass-bot

# Create and activate a virtual environment
python -m venv venv
# On Linux/macOS:
source venv/bin/activate
# On Windows:
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Edit `.env` with your actual bot credentials:
```env
BOT_TOKEN=1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ
BOT_USERNAME=@YourBotUsername
DB_PATH=fruit_pass.db
CLEANUP_ENABLED=false
```

### 4. Run the Bot
```bash
python -m fruit_pass.bot_app
```

---

## 🌐 Hosting & Deployment Guides

### Option 1: Docker Compose (Recommended)

1. Ensure Docker and Docker Compose are installed on your server.
2. Clone the repo and configure your `.env` file:
   ```bash
   cp .env.example .env
   nano .env
   ```
3. Start the bot container in background mode:
   ```bash
   docker compose up -d --build
   ```
4. View real-time logs:
   ```bash
   docker compose logs -f
   ```
*Data persistence: The SQLite database is automatically persisted to the `fruit_pass_data` Docker volume.*

---

### Option 2: Linux VPS (Ubuntu / Debian with systemd)

1. Connect to your VPS and install dependencies:
   ```bash
   sudo apt update && sudo apt install -y python3 python3-venv git
   ```
2. Clone the repository into `/opt/fruit-pass-bot`:
   ```bash
   sudo git clone https://github.com/your-username/fruit-pass-bot.git /opt/fruit-pass-bot
   cd /opt/fruit-pass-bot
   sudo python3 -m venv venv
   sudo /opt/fruit-pass-bot/venv/bin/pip install -r requirements.txt
   ```
3. Configure `.env`:
   ```bash
   sudo cp .env.example .env
   sudo nano .env
   ```
4. Install and enable the systemd service:
   ```bash
   sudo cp fruitpass.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now fruitpass
   ```
5. Check status and logs:
   ```bash
   sudo systemctl status fruitpass
   sudo journalctl -u fruitpass -f
   ```

---

### Option 3: Railway / Render / Fly.io

- **Procfile** is included: `worker: python -m fruit_pass.bot_app`.
- **Environment Variables**: Add `BOT_TOKEN`, `BOT_USERNAME`, `DB_PATH`, and `CLEANUP_ENABLED` in your platform's dashboard.
- Set the deployment type to **Background Worker** (since Telegram uses long polling, no HTTP port binding is needed).
- Mount a persistent disk/volume to `/data` and set `DB_PATH=/data/fruit_pass.db` to keep game states and scoreboards persistent across redeployments.

---

## 🤖 BotFather Setup Reference

To make the bot interactive in groups and inline mode:
1. Open [@BotFather](https://t.me/BotFather) on Telegram.
2. Run `/setinline` and choose your bot. Set inline placeholder: `Pass a card...`
3. Run `/setinlinefeedback` and select **Enabled (100%)**.
4. Run `/setjoingroups` and select **Enable**.
5. Run `/setcommands` and paste the list from [`botfather_guide.md`](botfather_guide.md).

For full details, see [botfather_guide.md](botfather_guide.md).

---

## 📂 Project Structure

```text
fruit-pass-bot/
├── fruit_pass/
│   ├── __init__.py
│   ├── bot_app.py           # Application entrypoint & polling lifecycle
│   ├── bot_handlers.py      # Commands, callbacks & inline query handlers
│   ├── bot_keyboards.py     # Inline markup buttons & turn panels
│   ├── bot_messages.py      # HTML formatting, announcements & strings
│   ├── config.py            # Environment settings & config flags
│   ├── database.py          # aiosqlite persistence & WAL mode
│   ├── game_engine.py       # Pure game domain, deck shuffling, victory checks
│   ├── game_service.py      # Concurrency locks, room coordination & state machine
│   ├── models.py            # Pydantic schemas (Player, Room, Card)
│   └── timer_service.py     # Background worker for 60s turn deadlines
├── tests/                   # Full test suite (69 test cases)
├── .env.example             # Safe environment variable template
├── .gitignore               # Ignored files (secrets, database, caches)
├── botfather_guide.md       # BotFather commands and description setup
├── Dockerfile               # Production multi-stage Docker build
├── docker-compose.yml       # 1-click Docker deployment
├── fruitpass.service        # Systemd daemon service unit
├── Procfile                 # Cloud worker configuration
├── requirements.txt         # Runtime dependencies
├── requirements-dev.txt     # Test & development dependencies
└── README.md                # Documentation & hosting guide
```

---

## 🧪 Running Tests

To run the complete automated test suite:

```bash
pip install -r requirements-dev.txt
pytest -v
```

All 69 test cases validate:
- Concurrency locks & simultaneous pass safety
- Host privileges and admin power checks
- Display names & notification pings
- Turn flow, auto-pass timer & turn panels
- Inline query secret card selection & automatic winner detection

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
