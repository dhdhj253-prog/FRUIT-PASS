import os
from typing import List, Tuple
from dotenv import load_dotenv

load_dotenv()

# Bot Identity
BOT_NAME = "Fruit Pass"
BOT_USERNAME = os.getenv("BOT_USERNAME", "FruitPassBot")
CLEAN_BOT_USERNAME = BOT_USERNAME.lstrip("@")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

TAGLINE = "Pass smart. Match four. Win the round."
SHORT_DESCRIPTION = "Fast card-passing game for 4 to 8 players. Collect four matching fruits to win!"
ABOUT_TEXT = (
    "Everyone starts with 4 fruit cards. On your turn, secretly pass one card to the next player "
    "and try to end up with four of the same fruit. Be the first to claim victory and win the round. "
    "Play 5, 8, or 10 rounds. Most points wins the match."
)
WELCOME_MESSAGE = (
    "🍎 Welcome to Fruit Pass! Collect four of the same fruit before anyone else. "
    "Pass one card each turn and keep your hand secret. "
    "Add me to a group and use /newgame to host, or /join to enter a room. "
    "You need 4 to 8 players."
)

# Game Rules Constants
MIN_PLAYERS = 4
MAX_PLAYERS = 8
DEFAULT_ROUNDS_OPTIONS = [5, 8, 10]
DEFAULT_TOTAL_ROUNDS = 5
TURN_TIMEOUT_SECONDS = 60
CONSECUTIVE_AUTOPASS_FLAG_THRESHOLD = 2
ABANDONED_ROOM_TIMEOUT_SECONDS = 3600  # 1 hour inactivity cleanup

# Fruit Card Ranks (Strict Rank Order)
# Rank index, Name, Emoji
FRUIT_RANKS: List[Tuple[int, str, str]] = [
    (0, "Apple", "🍎"),
    (1, "Mango", "🥭"),
    (2, "Grape", "🍇"),
    (3, "Cherry", "🍒"),
    (4, "Lemon", "🍋"),
    (5, "Peach", "🍑"),
    (6, "Berry", "🍓"),
    (7, "Melon", "🍈"),
]

# Quick lookup dicts
RANK_BY_INDEX = {rank: (name, emoji) for rank, name, emoji in FRUIT_RANKS}
RANK_NAME_BY_INDEX = {rank: name for rank, name, emoji in FRUIT_RANKS}
RANK_EMOJI_BY_INDEX = {rank: emoji for rank, name, emoji in FRUIT_RANKS}

# Database Path
DB_PATH = os.getenv("DB_PATH", "fruit_pass.db")

# Message Cleanup Configuration
CLEANUP_ENABLED = os.getenv("CLEANUP_ENABLED", "false").lower() in ("true", "1", "yes")
