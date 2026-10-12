from typing import List, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from fruit_pass.models import Room, Player, Card
from fruit_pass.config import BOT_USERNAME, DEFAULT_ROUNDS_OPTIONS, MIN_PLAYERS


def get_lobby_keyboard(room: Room, bot_username: str = BOT_USERNAME) -> InlineKeyboardMarkup:
    chat_id = room.chat_id
    rows: List[List[InlineKeyboardButton]] = []

    # Join via group button callback (no deep-link /start)
    join_btn = InlineKeyboardButton("➕ Join Room", callback_data=f"join:{chat_id}")
    leave_btn = InlineKeyboardButton("🚪 Leave", callback_data=f"leave:{chat_id}")
    rows.append([join_btn, leave_btn])

    # Round selection buttons with compact markers (e.g. "✅ 5", "8", "10")
    round_btns = [
        InlineKeyboardButton(
            f"✅ {r}" if r == room.total_rounds else f"{r}",
            callback_data=f"rnd:{chat_id}:{r}",
        )
        for r in DEFAULT_ROUNDS_OPTIONS
    ]
    rows.append(round_btns)

    # Host-only "▶️ Start Game" button if at least MIN_PLAYERS (4+)
    if len(room.active_players) >= MIN_PLAYERS:
        rows.append([InlineKeyboardButton("▶️ Start Game", callback_data=f"start:{chat_id}")])

    return InlineKeyboardMarkup(rows)


def get_table_keyboard(room: Room) -> InlineKeyboardMarkup:
    """
    Table message inline keyboard:
    - "🎴 My cards": switch_inline_query_current_chat so inline query opens in this chat.
    - "🏆 CLAIM VICTORY": public claim button (server validates every claim).
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎴 My cards", switch_inline_query_current_chat=""),
            InlineKeyboardButton("🏆 CLAIM VICTORY", callback_data=f"claim:{room.chat_id}:{room.turn_nonce}"),
        ]
    ])


def get_kill_confirmation_keyboard(room_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, end it", callback_data=f"killcnf:{room_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"killcan:{room_id}"),
        ]
    ])


def get_start_bot_keyboard(deep_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start Fruit Pass", url=deep_link)]
    ])


def get_remove_list_keyboard(room: Room) -> InlineKeyboardMarkup:
    removable = [p for p in room.players if p.user_id != room.host_id and not p.is_removed]
    rows: List[List[InlineKeyboardButton]] = []
    for p in removable:
        prefix = "⚠️ " if p.is_flagged else ""
        rows.append([
            InlineKeyboardButton(f"{prefix}{p.display_name}", callback_data=f"rmsel:{room.chat_id}:{p.user_id}")
        ])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"rmcan:{room.chat_id}")])
    return InlineKeyboardMarkup(rows)


def get_remove_confirm_keyboard(room_id: int, target_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Remove", callback_data=f"rmcnf:{room_id}:{target_user_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"rmcan:{room_id}"),
        ]
    ])


def get_leave_confirm_keyboard(room_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Leave", callback_data=f"lvcnf:{room_id}:{user_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"lvcan:{room_id}:{user_id}"),
        ]
    ])


def get_turn_panel_keyboard() -> InlineKeyboardMarkup:
    """
    One inline keyboard button below the turn panel message:
    text 'Make your choice!', switch_inline_query_current_chat=''
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Make your choice!", switch_inline_query_current_chat=""),
        ]
    ])
