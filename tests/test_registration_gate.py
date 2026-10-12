import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock
from telegram import Update, User, Chat, Message, CallbackQuery, InlineQuery, ChatMemberUpdated, ChatMemberMember, ChatMemberBanned

from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.database import Database
from fruit_pass.game_service import GameService
from fruit_pass.bot_handlers import BotHandlers
from fruit_pass.config import CLEAN_BOT_USERNAME, MAX_PLAYERS
import fruit_pass.bot_messages as msgs


@pytest.fixture
def temp_db():
    temp_dir = tempfile.TemporaryDirectory()
    db_file = os.path.join(temp_dir.name, "test_gate.db")
    db = Database(db_path=db_file)
    yield db
    temp_dir.cleanup()


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    bot.delete_message = AsyncMock(return_value=True)
    bot.edit_message_text = AsyncMock(return_value=True)
    return bot


# -------------------------------------------------------------
# 1. ➕ Join button from unregistered user
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_unregistered_user_pressing_join_button(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Create open room
    await temp_db.register_or_activate_user(1, "Host", "host")
    room, _ = await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # Unregistered user 99 presses ➕ Join
    update = MagicMock(spec=Update)
    query = AsyncMock(spec=CallbackQuery)
    query.data = "join:100"
    query.from_user = User(id=99, first_name="UnregisteredBob", is_bot=False)
    update.callback_query = query

    await handlers.handle_callback_query(update, MagicMock())

    # Must call answerCallbackQuery with deep link URL
    query.answer.assert_called_once()
    assert "url" in query.answer.call_args.kwargs
    assert f"https://t.me/{CLEAN_BOT_USERNAME}?start=join_100" == query.answer.call_args.kwargs["url"]

    # User 99 must NOT be seated
    loaded_room = await temp_db.load_room(100)
    assert not any(p.user_id == 99 for p in loaded_room.players)


# -------------------------------------------------------------
# 2. /join text command from unregistered user
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_unregistered_user_slash_join(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Create room
    await temp_db.register_or_activate_user(1, "Host", "host")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # Unregistered user 99 runs /join
    update = MagicMock(spec=Update)
    update.effective_chat = Chat(id=100, type="supergroup")
    update.effective_user = User(id=99, first_name="UnregisteredBob", is_bot=False)
    message = AsyncMock(spec=Message)
    update.effective_message = message

    await handlers.cmd_join(update, MagicMock())

    # Must reply with Start button
    message.reply_text.assert_called_once()
    reply_text = message.reply_text.call_args.args[0]
    reply_markup = message.reply_text.call_args.kwargs.get("reply_markup")

    assert "👋 Start the bot first to join the game." in reply_text
    assert reply_markup is not None
    # Inspect button
    button = reply_markup.inline_keyboard[0][0]
    assert button.text == "▶️ Start Fruit Pass"
    assert button.url == f"https://t.me/{CLEAN_BOT_USERNAME}?start=join_100"

    # User 99 must NOT be seated
    loaded_room = await temp_db.load_room(100)
    assert not any(p.user_id == 99 for p in loaded_room.players)


# -------------------------------------------------------------
# 3. /start join_<room_id> auto-seats when lobby open & not full
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_with_payload_auto_seats(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Room exists
    await temp_db.register_or_activate_user(1, "Host", "host")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # User 99 sends /start join_100
    update = MagicMock(spec=Update)
    update.effective_chat = Chat(id=99, type="private")
    update.effective_user = User(id=99, first_name="Bob", username="bob99", is_bot=False)
    message = AsyncMock(spec=Message)
    update.effective_message = message

    context = MagicMock()
    context.args = ["join_100"]

    await handlers.cmd_start(update, context)

    # 1. User 99 registered and active
    assert await temp_db.is_user_registered_and_active(99) is True

    # 2. Private chat confirmation with rules summary
    assert message.reply_text.called
    pm_text = message.reply_text.call_args.args[0]
    assert "✅ You're all set! Going back to the group to join the game." in pm_text
    assert "How to Play" in pm_text

    # 3. User 99 is seated in room 100
    room = await temp_db.load_room(100)
    assert any(p.user_id == 99 for p in room.players)

    # 4. Announcement sent to group
    mock_bot.send_message.assert_called_with(
        chat_id=100,
        text="➕ Bob joined (2/8 players)",
    )


# -------------------------------------------------------------
# 4. /start join_<room_id> when closed, full, or ended
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_with_payload_closed_full_ended(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Case A: Room does not exist
    update = MagicMock(spec=Update)
    update.effective_chat = Chat(id=88, type="private")
    update.effective_user = User(id=88, first_name="Dan", is_bot=False)
    message = AsyncMock(spec=Message)
    update.effective_message = message
    context = MagicMock()
    context.args = ["join_9999"]

    await handlers.cmd_start(update, context)
    calls = [call.args[0] for call in message.reply_text.call_args_list]
    assert any("That game is no longer open. Ask the host to start a new one." in c for c in calls)

    # Case B: Room is closed / lobby locked
    await temp_db.register_or_activate_user(1, "Host", "host")
    room, _ = await service.create_room(chat_id=200, host_id=1, host_first_name="Host")
    await service.set_lobby_lock(chat_id=200, requester_id=1, lock=True)

    message.reply_text.reset_mock()
    context.args = ["join_200"]
    await handlers.cmd_start(update, context)
    calls = [call.args[0] for call in message.reply_text.call_args_list]
    assert any("That game is no longer open. Ask the host to start a new one." in c for c in calls)

    # Case C: Room is full (8/8)
    message.reply_text.reset_mock()
    await service.set_lobby_lock(chat_id=200, requester_id=1, lock=False)
    for i in range(2, 9):
        await temp_db.register_or_activate_user(i, f"P{i}")
        await service.join_room(200, user_id=i, first_name=f"P{i}")

    await handlers.cmd_start(update, context)
    calls = [call.args[0] for call in message.reply_text.call_args_list]
    assert any("That game is no longer open. Ask the host to start a new one." in c for c in calls)


# -------------------------------------------------------------
# 5. Blocked user must restart bot before joining
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_blocked_user_must_restart_bot(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Create room
    await temp_db.register_or_activate_user(1, "Host", "host")
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # User 50 registers
    await temp_db.register_or_activate_user(50, "Eve")
    assert await temp_db.is_user_registered_and_active(50) is True

    # User 50 blocks bot (my_chat_member status: kicked)
    member_update = MagicMock(spec=Update)
    member_update.my_chat_member = MagicMock(spec=ChatMemberUpdated)
    member_update.my_chat_member.chat = Chat(id=50, type="private")
    member_update.my_chat_member.new_chat_member = MagicMock()
    member_update.my_chat_member.new_chat_member.status = "kicked"

    await handlers.handle_my_chat_member(member_update, MagicMock())
    assert await temp_db.is_user_registered_and_active(50) is False

    # Blocked user 50 tries to /join -> rejected with Start button!
    update = MagicMock(spec=Update)
    update.effective_chat = Chat(id=100, type="supergroup")
    update.effective_user = User(id=50, first_name="Eve", is_bot=False)
    msg = AsyncMock(spec=Message)
    update.effective_message = msg

    await handlers.cmd_join(update, MagicMock())
    msg.reply_text.assert_called_once()
    assert "👋 Start the bot first to join the game." in msg.reply_text.call_args.args[0]

    # Blocked user 50 restarts the bot (/start)
    start_update = MagicMock(spec=Update)
    start_update.effective_chat = Chat(id=50, type="private")
    start_update.effective_user = User(id=50, first_name="Eve", is_bot=False)
    start_msg = AsyncMock(spec=Message)
    start_update.effective_message = start_msg
    start_context = MagicMock()
    start_context.args = []

    await handlers.cmd_start(start_update, start_context)
    # Status is active again!
    assert await temp_db.is_user_registered_and_active(50) is True

    # User 50 can now /join
    msg.reply_text.reset_mock()
    await handlers.cmd_join(update, MagicMock())
    room = await temp_db.load_room(100)
    assert any(p.user_id == 50 for p in room.players)


# -------------------------------------------------------------
# 6. Seated players are unaffected by gate
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_seated_players_unaffected_by_gate(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # 4 registered seated players in match
    for i in range(1, 5):
        await temp_db.register_or_activate_user(i, f"P{i}")

    players = [
        Player(user_id=i, room_id=300, first_name=f"P{i}", seat_order=i - 1)
        for i in range(1, 5)
    ]
    room = Room(chat_id=300, host_id=1, players=players)
    from fruit_pass.game_engine import GameEngine
    GameEngine.deal_round(room, round_number=1)
    await temp_db.save_room(room)

    # Active player can view hand inline without disruption
    active = room.get_active_player()
    iq_update = MagicMock(spec=Update)
    iq = AsyncMock(spec=InlineQuery)
    iq.from_user = User(id=active.user_id, first_name=active.first_name, is_bot=False)
    iq_update.inline_query = iq

    await handlers.handle_inline_query(iq_update, MagicMock())
    iq.answer.assert_called_once()
    results = iq.answer.call_args.kwargs.get("results")
    assert len(results) > 0


# -------------------------------------------------------------
# 7. Mid-match queued joiners are also gated
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_mid_match_queued_joiners_gated(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    for i in range(1, 5):
        await temp_db.register_or_activate_user(i, f"P{i}")

    room, _ = await service.create_room(chat_id=400, host_id=1, host_first_name="P1")
    for i in range(2, 5):
        await service.join_room(400, user_id=i, first_name=f"P{i}")

    await service.start_match(400, requester_id=1)
    loaded = await temp_db.load_room(400)
    assert loaded.state == RoomState.PLAYING

    # Unregistered user 77 presses ➕ Join during match
    cb_update = MagicMock(spec=Update)
    cb_query = AsyncMock(spec=CallbackQuery)
    cb_query.data = "join:400"
    cb_query.from_user = User(id=77, first_name="UnregisteredMid", is_bot=False)
    cb_update.callback_query = cb_query

    await handlers.handle_callback_query(cb_update, MagicMock())
    # Gated with deep link URL
    cb_query.answer.assert_called_once()
    assert "url" in cb_query.answer.call_args.kwargs

    # Not in waiting queue
    loaded2 = await temp_db.load_room(400)
    assert not any(p.user_id == 77 for p in loaded2.players)

    # Now register user 77
    await temp_db.register_or_activate_user(77, "RegisteredMid")
    cb_query.answer.reset_mock()

    await handlers.handle_callback_query(cb_update, MagicMock())
    # Seated into waiting queue
    loaded3 = await temp_db.load_room(400)
    p77 = loaded3.get_player(77)
    assert p77 is not None
    assert p77.is_waiting is True


# -------------------------------------------------------------
# 8. Unregistered host on /newgame
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_unregistered_host_newgame(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    update = MagicMock(spec=Update)
    update.effective_chat = Chat(id=500, type="group")
    update.effective_user = User(id=888, first_name="NewHost", is_bot=False)
    msg = AsyncMock(spec=Message)
    update.effective_message = msg

    await handlers.cmd_newgame(update, MagicMock())

    # Host gated
    msg.reply_text.assert_called_once()
    assert "👋 Start the bot first to join the game." in msg.reply_text.call_args.args[0]
    button = msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.url == f"https://t.me/{CLEAN_BOT_USERNAME}?start=newgame"
