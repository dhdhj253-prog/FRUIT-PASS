import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram import Update, User, Chat, Message, CallbackQuery, InlineKeyboardMarkup
from telegram.error import TelegramError, Forbidden

from fruit_pass.models import Room, RoomState, Player
from fruit_pass.game_service import GameService
from fruit_pass.bot_handlers import BotHandlers
import fruit_pass.bot_messages as msgs
import fruit_pass.bot_keyboards as kb
from fruit_pass.config import BOT_USERNAME


import os
import tempfile
from fruit_pass.database import Database


@pytest.fixture
def temp_db():
    temp_dir = tempfile.TemporaryDirectory()
    db_file = os.path.join(temp_dir.name, "test_bugfixes.db")
    db = Database(db_path=db_file)
    yield db
    temp_dir.cleanup()


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=999))
    bot.delete_message = AsyncMock(return_value=True)
    bot.send_chat_action = AsyncMock(return_value=True)
    return bot


# ---------------------------------------------------------------------------
# TEST 1: Five users tapping Join concurrently
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_five_users_concurrent_join(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)

    # Host creates room (1 player)
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # 5 users join at the exact same moment
    async def join_user(uid):
        return await service.join_room(
            chat_id=100,
            user_id=uid,
            first_name=f"Player_{uid}",
            username=f"user_{uid}",
        )

    results = await asyncio.gather(*[join_user(uid) for uid in range(2, 7)])

    # All 5 joins succeeded
    for room, msg in results:
        assert room is not None
        assert msg == "Joined successfully."

    # Room has exactly 6 players (host + 5)
    final_room = await temp_db.load_room(100)
    assert len(final_room.players) == 6
    assert len(final_room.active_players) == 6

    # Verify database room_players table directly
    db_players = await temp_db.get_room_players(100)
    assert len(db_players) == 6
    user_ids = {p["user_id"] for p in db_players}
    assert user_ids == {1, 2, 3, 4, 5, 6}

    # Lobby text shows real player list and "Players (6/8):"
    lobby_text = msgs.get_lobby_text(final_room)
    assert "Players (6/8):" in lobby_text
    assert "Seating & Turn Order" not in lobby_text
    for uid in range(2, 7):
        assert f"Player_{uid}" in lobby_text


# ---------------------------------------------------------------------------
# TEST 2: User who started bot earlier (not in registry) joins via probe
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unregistered_user_auto_probe_joins(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)

    await service.create_room(chat_id=200, host_id=1, host_first_name="Host")

    # User 88 is NOT in database
    assert await temp_db.get_user(88) is None

    # Probe succeeds (send_chat_action works)
    is_active = await service.check_user_registered_or_probe(88, "Charlie", "charlie88")
    assert is_active is True

    # User is now registered as active in DB
    user_rec = await temp_db.get_user(88)
    assert user_rec is not None
    assert user_rec["status"] == "active"

    # User joins room with check_registration=True without being blocked
    room, msg = await service.join_room(
        chat_id=200,
        user_id=88,
        first_name="Charlie",
        username="charlie88",
        check_registration=True,
    )
    assert room is not None
    assert msg == "Joined successfully."

    # Probe failure with Forbidden (403): user never started or blocked
    mock_bot.send_chat_action.side_effect = Forbidden("Forbidden: bot can't initiate conversation")
    is_active_blocked = await service.check_user_registered_or_probe(99, "Dave", "dave99")
    assert is_active_blocked is False

    room_fail, msg_fail = await service.join_room(
        chat_id=200,
        user_id=99,
        first_name="Dave",
        username="dave99",
        check_registration=True,
    )
    assert room_fail is None
    assert msg_fail == "Start the bot first to join the game."


# ---------------------------------------------------------------------------
# TEST 3: /newgame after a finished or killed game starts empty with creator
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_newgame_after_finished_or_killed_starts_clean(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)

    # Game 1: host 1 and players 2, 3, 4, 5
    await service.create_room(chat_id=300, host_id=1, host_first_name="Host1")
    for uid in range(2, 6):
        await service.join_room(chat_id=300, user_id=uid, first_name=f"P_{uid}")

    old_room = await temp_db.load_room(300)
    assert len(old_room.players) == 5

    # Host ends the game via kill_room
    success, _, _ = await service.kill_room(chat_id=300, requester_id=1)
    assert success is True

    # New creator 42 runs /newgame in that same group chat 300
    new_room, created_new = await service.create_room(
        chat_id=300,
        host_id=42,
        host_first_name="NewHost",
        host_username="new_host",
    )
    assert created_new is True
    assert new_room.chat_id == 300
    assert new_room.host_id == 42
    assert len(new_room.players) == 1
    assert new_room.players[0].user_id == 42
    assert new_room.players[0].first_name == "NewHost"

    # Verify room_players in DB contains only new host 42
    db_players = await temp_db.get_room_players(300)
    assert len(db_players) == 1
    assert db_players[0]["user_id"] == 42


# ---------------------------------------------------------------------------
# TEST 4: No false "already joined" after a game ends, and /newgame blocked if active
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_false_already_joined_and_newgame_guards(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Room 400 active
    await service.create_room(chat_id=400, host_id=1, host_first_name="Host")
    await service.join_room(chat_id=400, user_id=2, first_name="Player2")

    # While room 400 is active, /newgame is rejected
    update = MagicMock(spec=Update)
    update.effective_chat = Chat(id=400, type="group")
    update.effective_user = User(id=3, first_name="Player3", is_bot=False)
    message = AsyncMock(spec=Message)
    update.effective_message = message

    await handlers.cmd_newgame(update, MagicMock())
    message.reply_text.assert_called_with(
        "A game is already in progress. The host can use /kill to end it first."
    )

    # Host kills room 400
    await service.kill_room(chat_id=400, requester_id=1)

    # Player 2 can now join a brand new room 500 without false "already joined"
    await service.create_room(chat_id=500, host_id=10, host_first_name="Host10")
    room, msg = await service.join_room(chat_id=500, user_id=2, first_name="Player2")
    assert room is not None
    assert msg == "Joined successfully."

    # If Player 2 attempts to join room 500 again, they get "You're already in this game."
    room_dup, msg_dup = await service.join_room(chat_id=500, user_id=2, first_name="Player2")
    assert msg_dup == "You're already in this game."


# ---------------------------------------------------------------------------
# TEST 5: /start in group starts match for host with 4+ players, private onboards
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_group_start_host_only_and_private_start_onboarding(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Create room in group 600
    await service.create_room(chat_id=600, host_id=1, host_first_name="Host")
    await service.join_room(chat_id=600, user_id=2, first_name="P2")
    await service.join_room(chat_id=600, user_id=3, first_name="P3")

    # 1. Non-host runs /start in group
    update_non_host = MagicMock(spec=Update)
    update_non_host.effective_chat = Chat(id=600, type="group")
    update_non_host.effective_user = User(id=2, first_name="P2", is_bot=False)
    msg_non_host = AsyncMock(spec=Message)
    update_non_host.effective_message = msg_non_host

    await handlers.cmd_start(update_non_host, MagicMock())
    msg_non_host.reply_text.assert_called_with("Only the host or a group admin can do that.")

    # 2. Host runs /start with only 3 players
    update_host = MagicMock(spec=Update)
    update_host.effective_chat = Chat(id=600, type="group")
    update_host.effective_user = User(id=1, first_name="Host", is_bot=False)
    msg_host = AsyncMock(spec=Message)
    update_host.effective_message = msg_host

    await handlers.cmd_start(update_host, MagicMock())
    msg_host.reply_text.assert_called_with("Need at least 4 players (now 3).")

    # 3. 4th player joins, host runs /start -> match starts!
    await service.join_room(chat_id=600, user_id=4, first_name="P4")
    await handlers.cmd_start(update_host, MagicMock())

    started_room = await temp_db.load_room(600)
    assert started_room.state == RoomState.PLAYING
    assert started_room.current_round == 1

    # 4. In private chat, /start onboards
    update_pm = MagicMock(spec=Update)
    update_pm.effective_chat = Chat(id=999, type="private")
    update_pm.effective_user = User(id=999, first_name="Newbie", is_bot=False)
    msg_pm = AsyncMock(spec=Message)
    update_pm.effective_message = msg_pm

    context_pm = MagicMock()
    context_pm.args = []
    await handlers.cmd_start(update_pm, context_pm)

    msg_pm.reply_text.assert_called_with(msgs.get_welcome_text(), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# TEST 6: Start Game button appears and works with 4 or more players
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_game_button_keyboard_and_callback(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # 3 players: keyboard should not have Start Game button
    room, _ = await service.create_room(chat_id=700, host_id=1, host_first_name="Host")
    await service.join_room(chat_id=700, user_id=2, first_name="P2")
    await service.join_room(chat_id=700, user_id=3, first_name="P3")

    room = await temp_db.load_room(700)
    kb3 = kb.get_lobby_keyboard(room)
    button_texts_3 = [btn.text for row in kb3.inline_keyboard for btn in row]
    assert "▶️ Start Game" not in button_texts_3

    # Check compact round buttons (e.g. ✅ 5, 8, 10)
    assert "✅ 5" in button_texts_3
    assert "8" in button_texts_3
    assert "10" in button_texts_3

    # Add 4th player: button appears
    await service.join_room(chat_id=700, user_id=4, first_name="P4")
    room = await temp_db.load_room(700)
    kb4 = kb.get_lobby_keyboard(room)
    button_texts_4 = [btn.text for row in kb4.inline_keyboard for btn in row]
    assert "▶️ Start Game" in button_texts_4

    # Non-host tapping Start Game callback
    update_cb = MagicMock(spec=Update)
    query = AsyncMock(spec=CallbackQuery)
    query.data = "start:700"
    query.from_user = User(id=2, first_name="P2", is_bot=False)
    update_cb.callback_query = query

    await handlers.handle_callback_query(update_cb, MagicMock())
    query.answer.assert_called_with("Only the host or a group admin can do that.", show_alert=True)

    # Host tapping Start Game callback -> starts match!
    query.from_user = User(id=1, first_name="Host", is_bot=False)
    await handlers.handle_callback_query(update_cb, MagicMock())

    started_room = await temp_db.load_room(700)
    assert started_room.state == RoomState.PLAYING


# ---------------------------------------------------------------------------
# TEST 7: Lobby message edit failures recover without crashing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lobby_message_edit_failure_recovery(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)

    room, _ = await service.create_room(chat_id=800, host_id=1, host_first_name="Host")
    room.lobby_message_id = 555
    await temp_db.save_room(room)

    # edit_message_text fails with "Message to edit not found"
    mock_bot.edit_message_text.side_effect = TelegramError("Bad Request: message to edit not found")
    mock_bot.send_message.return_value = MagicMock(message_id=777)

    # Trigger lobby update
    await service.update_lobby_message(800)

    # Recovery: new message was sent and new lobby_message_id stored
    mock_bot.send_message.assert_called()
    updated_room = await temp_db.load_room(800)
    assert updated_room.lobby_message_id == 777

    # Quiet handling of "message is not modified"
    mock_bot.edit_message_text.side_effect = TelegramError("Bad Request: message is not modified")
    # Should complete without error and without sending new message
    mock_bot.send_message.reset_mock()
    await service.update_lobby_message(800)
    mock_bot.send_message.assert_not_called()
