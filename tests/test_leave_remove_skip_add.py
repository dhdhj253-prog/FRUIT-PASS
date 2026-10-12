import asyncio
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram import Update, User, Chat, Message, CallbackQuery, MessageEntity, InlineKeyboardMarkup

from fruit_pass.models import Room, RoomState, Player, Card
from fruit_pass.game_service import GameService
from fruit_pass.bot_handlers import BotHandlers
from fruit_pass.database import Database
import fruit_pass.bot_messages as msgs
import fruit_pass.bot_keyboards as kb
from fruit_pass.config import BOT_USERNAME


@pytest.fixture
def temp_db():
    temp_dir = tempfile.TemporaryDirectory()
    db_file = os.path.join(temp_dir.name, "test_lrsa.db")
    db = Database(db_path=db_file)
    yield db
    temp_dir.cleanup()


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=999))
    bot.delete_message = AsyncMock(return_value=True)
    bot.get_chat_member = AsyncMock()
    bot.send_chat_action = AsyncMock(return_value=True)
    return bot


def make_chat_member(status="member"):
    m = MagicMock()
    m.status = status
    return m


def make_group_update(chat_id=100, user_id=1, first_name="User", text="", reply_to=None, entities=None):
    update = MagicMock(spec=Update)
    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    chat.type = "supergroup"
    update.effective_chat = chat

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = first_name
    user.last_name = None
    user.username = f"user_{user_id}"
    user.is_bot = False
    update.effective_user = user

    msg = MagicMock(spec=Message)
    msg.message_id = 555
    msg.text = text
    msg.reply_text = AsyncMock(return_value=MagicMock(message_id=777))
    msg.sender_chat = None
    msg.reply_to_message = reply_to
    msg.entities = entities or []

    update.effective_message = msg
    update.callback_query = None
    return update


def make_callback_update(chat_id=100, user_id=1, first_name="User", data=""):
    update = MagicMock(spec=Update)
    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    chat.type = "supergroup"
    update.effective_chat = chat

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = first_name
    user.last_name = None
    user.username = f"user_{user_id}"
    user.is_bot = False
    update.effective_user = user

    query = MagicMock(spec=CallbackQuery)
    query.data = data
    query.from_user = user
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock(spec=Message)
    query.message.message_id = 444
    query.message.reply_text = AsyncMock(return_value=MagicMock(message_id=888))

    update.callback_query = query
    update.effective_message = query.message
    return update


# ===========================================================================
# 1. /leave tests (command & button, lobby, mid-match, transfer, void, pause, lock release)
# ===========================================================================
@pytest.mark.asyncio
async def test_leave_not_in_game(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Room exists with Host
    await temp_db.register_or_activate_user(1, "Host")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # User 99 (not in room) runs /leave
    update = make_group_update(chat_id=100, user_id=99, first_name="Stranger", text="/leave")
    await handlers.cmd_leave(update, MagicMock())
    update.effective_message.reply_text.assert_called_with("You're not in this game.")

    # User 99 presses leave button
    cb = make_callback_update(chat_id=100, user_id=99, first_name="Stranger", data="leave:100")
    await handlers.handle_callback_query(cb, MagicMock())
    cb.callback_query.answer.assert_called_with("You're not in this game.", show_alert=True)


@pytest.mark.asyncio
async def test_leave_lobby_regular_player(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    await temp_db.register_or_activate_user(1, "Host")
    await temp_db.register_or_activate_user(2, "Player2")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    await service.join_room(chat_id=100, user_id=2, first_name="Player2")

    assert await temp_db.get_active_room_for_user(2) is not None

    # Player 2 leaves via /leave
    update = make_group_update(chat_id=100, user_id=2, first_name="Player2", text="/leave")
    await handlers.cmd_leave(update, MagicMock())

    # Check announcement sent to group: "🚪 Player2 has left the game. (1/8)"
    mock_bot.send_message.assert_called_with(
        chat_id=100,
        text="🚪 Player2 has left the game. (1/8)",
        parse_mode="HTML",
    )

    # Active room lock released
    assert await temp_db.get_active_room_for_user(2) is None

    # Player 2 removed from room
    room = await temp_db.load_room(100)
    assert len(room.players) == 1
    assert room.players[0].user_id == 1


@pytest.mark.asyncio
async def test_leave_lobby_host_transfer_and_empty_room_delete(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Host 1 and Player 2
    await temp_db.register_or_activate_user(1, "Host1")
    await temp_db.register_or_activate_user(2, "Player2")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host1")
    await service.join_room(chat_id=100, user_id=2, first_name="Player2")

    # Host 1 leaves via button
    cb = make_callback_update(chat_id=100, user_id=1, first_name="Host1", data="leave:100")
    await handlers.handle_callback_query(cb, MagicMock())

    # Host transferred to Player 2
    room = await temp_db.load_room(100)
    assert room.host_id == 2
    assert len(room.players) == 1

    # Check announcements: leave + host transfer
    calls = [call.kwargs.get("text") for call in mock_bot.send_message.call_args_list]
    assert any("🚪 Host1 has left the game. (1/8)" in str(c) for c in calls)
    assert any("👑 Player2 is now the host." in str(c) for c in calls)

    # Now Player 2 (last remaining) leaves
    cb2 = make_callback_update(chat_id=100, user_id=2, first_name="Player2", data="leave:100")
    await handlers.handle_callback_query(cb2, MagicMock())

    # Room deleted because nobody remains
    assert await temp_db.load_room(100) is None
    assert await temp_db.get_active_room_for_user(2) is None


@pytest.mark.asyncio
async def test_leave_mid_match_confirmation_void_and_pause(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    for uid in range(1, 6):  # 5 players
        await temp_db.register_or_activate_user(uid, f"P{uid}")
    await service.create_room(chat_id=100, host_id=1, host_first_name="P1")
    for uid in range(2, 6):
        await service.join_room(chat_id=100, user_id=uid, first_name=f"P{uid}")
    await service.start_match(chat_id=100, requester_id=1)

    room = await temp_db.load_room(100)
    assert room.state == RoomState.PLAYING

    # P5 calls /leave mid-match -> should ask for confirmation
    update = make_group_update(chat_id=100, user_id=5, first_name="P5", text="/leave")
    await handlers.cmd_leave(update, MagicMock())
    assert update.effective_message.reply_text.called
    conf_msg = update.effective_message.reply_text.call_args[0][0]
    assert "Leaving will restart the current round for everyone. Leave? ✅ / ❌" in conf_msg

    # If P5 cancels:
    can_cb = make_callback_update(chat_id=100, user_id=5, first_name="P5", data="lvcan:100:5")
    await handlers.handle_callback_query(can_cb, MagicMock())
    assert "Leaving cancelled." in can_cb.callback_query.edit_message_text.call_args[0][0]
    room = await temp_db.load_room(100)
    assert not room.get_player(5).is_removed

    # Now P5 confirms leave (5 -> 4 players, still >= 4: should void and redeal)
    cnf_cb = make_callback_update(chat_id=100, user_id=5, first_name="P5", data="lvcnf:100:5")
    await handlers.handle_callback_query(cnf_cb, MagicMock())

    room = await temp_db.load_room(100)
    p5 = room.get_player(5)
    assert p5.is_left is True
    assert p5.is_removed is True
    assert room.state == RoomState.PLAYING  # 4 players remain, round redealt
    assert await temp_db.get_active_room_for_user(5) is None  # Lock released

    # Scoreboard shows P5 marked "(left)"
    sb = msgs.get_scoreboard_text(room)
    assert "P5" in sb and "(left)" in sb

    # Now P4 leaves mid-match (4 -> 3 players, < 4: should pause match)
    cnf_cb4 = make_callback_update(chat_id=100, user_id=4, first_name="P4", data="lvcnf:100:4")
    await handlers.handle_callback_query(cnf_cb4, MagicMock())

    room = await temp_db.load_room(100)
    assert room.state == RoomState.PAUSED  # Paused because < 4 players


# ===========================================================================
# 2. /remove tests (privileged, lists all non-host, confirmation, lobby & mid-match)
# ===========================================================================
@pytest.mark.asyncio
async def test_remove_unauthorized(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    await temp_db.register_or_activate_user(1, "Host")
    await temp_db.register_or_activate_user(2, "Player2")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    await service.join_room(chat_id=100, user_id=2, first_name="Player2")

    # Regular member (user 2) tries /remove
    mock_bot.get_chat_member.return_value = make_chat_member("member")
    update = make_group_update(chat_id=100, user_id=2, first_name="Player2", text="/remove")
    await handlers.cmd_remove(update, MagicMock())
    update.effective_message.reply_text.assert_called_with("Only the host or a group admin can do that.")


@pytest.mark.asyncio
async def test_remove_no_other_players(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Only host in room
    await temp_db.register_or_activate_user(1, "Host")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    update = make_group_update(chat_id=100, user_id=1, first_name="Host", text="/remove")
    await handlers.cmd_remove(update, MagicMock())
    update.effective_message.reply_text.assert_called_with("No other players to remove.")


@pytest.mark.asyncio
async def test_remove_lists_all_non_host_and_removes(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    await temp_db.register_or_activate_user(1, "Host")
    await temp_db.register_or_activate_user(2, "Player2")
    await temp_db.register_or_activate_user(3, "Player3")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    await service.join_room(chat_id=100, user_id=2, first_name="Player2")
    await service.join_room(chat_id=100, user_id=3, first_name="Player3")

    room = await temp_db.load_room(100)
    room.get_player(3).is_flagged = True  # Player 3 flagged
    await temp_db.save_room(room)

    # Host runs /remove
    update = make_group_update(chat_id=100, user_id=1, first_name="Host", text="/remove")
    await handlers.cmd_remove(update, MagicMock())

    reply_markup = update.effective_message.reply_text.call_args.kwargs.get("reply_markup")
    assert reply_markup is not None
    # Check buttons: Player2 and ⚠️ Player3
    btn_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "Player2" in btn_texts
    assert "⚠️ Player3" in btn_texts
    assert "Host" not in btn_texts

    # Host selects Player2
    sel_cb = make_callback_update(chat_id=100, user_id=1, first_name="Host", data="rmsel:100:2")
    await handlers.handle_callback_query(sel_cb, MagicMock())
    assert "Remove Player2?" in sel_cb.callback_query.edit_message_text.call_args[0][0]

    # Host confirms removal
    cnf_cb = make_callback_update(chat_id=100, user_id=1, first_name="Host", data="rmcnf:100:2")
    await handlers.handle_callback_query(cnf_cb, MagicMock())

    # Announcement: "🚫 Player2 was removed by Host."
    assert "🚫 Player2 was removed by Host." in cnf_cb.callback_query.edit_message_text.call_args[0][0]
    room = await temp_db.load_room(100)
    assert not any(p.user_id == 2 for p in room.players)


# ===========================================================================
# 3. /skip tests (privileged, only active turn, passes card, announces, no flag)
# ===========================================================================
@pytest.mark.asyncio
async def test_skip_not_in_turn_or_unprivileged(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # In lobby: no active turn
    await temp_db.register_or_activate_user(1, "Host")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    update = make_group_update(chat_id=100, user_id=1, first_name="Host", text="/skip")
    await handlers.cmd_skip(update, MagicMock())
    update.effective_message.reply_text.assert_called_with("There's no active turn to skip.")


@pytest.mark.asyncio
async def test_skip_active_turn_passes_and_no_flag(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    for uid in range(1, 5):
        await temp_db.register_or_activate_user(uid, f"P{uid}")
    await service.create_room(chat_id=100, host_id=1, host_first_name="P1")
    for uid in range(2, 5):
        await service.join_room(chat_id=100, user_id=uid, first_name=f"P{uid}")
    await service.start_match(chat_id=100, requester_id=1)

    room = await temp_db.load_room(100)
    active_player = room.get_active_player()
    active_id = active_player.user_id
    initial_strikes = active_player.consecutive_autopasses

    # Host executes /skip
    update = make_group_update(chat_id=100, user_id=1, first_name="P1", text="/skip")
    await handlers.cmd_skip(update, MagicMock())

    # Turn moved to next player
    updated_room = await temp_db.load_room(100)
    assert updated_room.active_player_id != active_id

    # The skipped player's strikes NOT increased
    skipped_p = updated_room.get_player(active_id)
    assert skipped_p.consecutive_autopasses == initial_strikes
    assert not skipped_p.is_flagged

    # Announcement: "⏭ [Name]'s turn was skipped by P1."
    calls = [call.kwargs.get("text") for call in mock_bot.send_message.call_args_list]
    assert any(f"⏭ {skipped_p.first_name}'s turn was skipped by P1." in str(c) for c in calls)


# ===========================================================================
# 4. /add tests (privileged, reply, mention, username, gate, duplicates, full)
# ===========================================================================
@pytest.mark.asyncio
async def test_add_unprivileged(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    await temp_db.register_or_activate_user(1, "Host")
    await temp_db.register_or_activate_user(2, "Player2")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    await service.join_room(chat_id=100, user_id=2, first_name="Player2")

    mock_bot.get_chat_member.return_value = make_chat_member("member")
    update = make_group_update(chat_id=100, user_id=2, first_name="Player2", text="/add")
    await handlers.cmd_add(update, MagicMock())
    update.effective_message.reply_text.assert_called_with("Only the host or a group admin can do that.")


@pytest.mark.asyncio
async def test_add_unresolved_and_bot_rejection(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    await temp_db.register_or_activate_user(1, "Host")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # Unresolved: no reply, no mention, unknown username
    ctx = MagicMock()
    ctx.args = ["@nonexistent_user"]
    update = make_group_update(chat_id=100, user_id=1, first_name="Host", text="/add @nonexistent_user")
    await handlers.cmd_add(update, ctx)
    update.effective_message.reply_text.assert_called_with(
        "I couldn't find that person. Reply to their message with /add, or ask them to start the bot first."
    )

    # Target is a bot
    bot_user = MagicMock(spec=User)
    bot_user.id = 888
    bot_user.is_bot = True
    bot_user.first_name = "SomeBot"
    reply_msg = MagicMock(spec=Message)
    reply_msg.from_user = bot_user
    update_bot = make_group_update(chat_id=100, user_id=1, first_name="Host", text="/add", reply_to=reply_msg)
    ctx.args = []
    await handlers.cmd_add(update_bot, ctx)
    update_bot.effective_message.reply_text.assert_called_with("Bots can't be added.")


@pytest.mark.asyncio
async def test_add_unregistered_prompts_start_gate(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    await temp_db.register_or_activate_user(1, "Host")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # Target user 77 is NOT registered in DB
    target_user = MagicMock(spec=User)
    target_user.id = 77
    target_user.is_bot = False
    target_user.first_name = "UnregisteredDan"
    target_user.last_name = None
    target_user.username = "dan77"

    from telegram.error import TelegramError
    mock_bot.send_chat_action.side_effect = TelegramError("Forbidden: bot can't initiate conversation with a user")

    reply_msg = MagicMock(spec=Message)
    reply_msg.from_user = target_user
    update = make_group_update(chat_id=100, user_id=1, first_name="Host", text="/add", reply_to=reply_msg)
    ctx = MagicMock()
    ctx.args = []

    await handlers.cmd_add(update, ctx)
    reply_call = update.effective_message.reply_text.call_args
    assert "UnregisteredDan needs to start the bot first." in reply_call[0][0]
    keyboard = reply_call.kwargs.get("reply_markup")
    assert keyboard is not None
    assert "https://t.me/FruitPassBot?start=join_100" in keyboard.inline_keyboard[0][0].url


@pytest.mark.asyncio
async def test_add_success_lobby_and_mid_match_and_duplicates(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    await temp_db.register_or_activate_user(1, "Host")
    await temp_db.register_or_activate_user(2, "Player2", "p2username")
    await temp_db.register_or_activate_user(3, "Player3", "p3username")
    await temp_db.register_or_activate_user(4, "Player4", "p4username")

    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # Add Player2 in lobby via username
    ctx = MagicMock()
    ctx.args = ["@p2username"]
    update = make_group_update(chat_id=100, user_id=1, first_name="Host", text="/add @p2username")
    await handlers.cmd_add(update, ctx)

    room = await temp_db.load_room(100)
    assert any(p.user_id == 2 for p in room.players)
    mock_bot.send_message.assert_called_with(
        chat_id=100,
        text="➕ Player2 was added to the game by Host. (2/8 players)",
        parse_mode="HTML",
    )

    # Duplicate: Add Player2 again
    update_dup = make_group_update(chat_id=100, user_id=1, first_name="Host", text="/add @p2username")
    await handlers.cmd_add(update_dup, ctx)
    update_dup.effective_message.reply_text.assert_called_with("Player2 is already in the game.")

    # Target in another room
    await service.create_room(chat_id=200, host_id=3, host_first_name="Player3")
    ctx.args = ["@p3username"]
    update_other = make_group_update(chat_id=100, user_id=1, first_name="Host", text="/add @p3username")
    await handlers.cmd_add(update_other, ctx)
    update_other.effective_message.reply_text.assert_called_with("Player3 is already in another game.")


# ===========================================================================
# 5. /join and button announcement & lobby panel update
# ===========================================================================
@pytest.mark.asyncio
async def test_join_command_and_button_unified(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    await temp_db.register_or_activate_user(1, "Host")
    await temp_db.register_or_activate_user(2, "Alice")
    await temp_db.register_or_activate_user(3, "Bob")

    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # Alice joins via command /join
    update_alice = make_group_update(chat_id=100, user_id=2, first_name="Alice", text="/join")
    await handlers.cmd_join(update_alice, MagicMock())

    # Announcement in group
    mock_bot.send_message.assert_called_with(
        chat_id=100,
        text="➕ Alice has joined the game! (2/8 players)",
        parse_mode="HTML",
    )

    # Bob joins via button callback
    cb_bob = make_callback_update(chat_id=100, user_id=3, first_name="Bob", data="join:100")
    await handlers.handle_callback_query(cb_bob, MagicMock())

    # Announcement in group
    mock_bot.send_message.assert_called_with(
        chat_id=100,
        text="➕ Bob has joined the game! (3/8 players)",
        parse_mode="HTML",
    )

    # Repeat join by Bob gets "You're already in this game."
    cb_bob_dup = make_callback_update(chat_id=100, user_id=3, first_name="Bob", data="join:100")
    await handlers.handle_callback_query(cb_bob_dup, MagicMock())
    cb_bob_dup.callback_query.answer.assert_called_with("You're already in this game.", show_alert=True)

    # Verify lobby panel text format
    room = await temp_db.load_room(100)
    lobby_text = msgs.get_lobby_text(room)
    assert "Players (3/8):" in lobby_text
    assert "Host (Host)" in lobby_text
    assert "Alice" in lobby_text
    assert "Bob" in lobby_text
