import asyncio
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram import Update, User, Chat, Message, CallbackQuery, ChatMemberUpdated, ChatMember

from fruit_pass.models import Room, RoomState, Player
from fruit_pass.game_service import GameService
from fruit_pass.bot_handlers import BotHandlers
from fruit_pass.database import Database
import fruit_pass.bot_messages as msgs


@pytest.fixture
def temp_db():
    temp_dir = tempfile.TemporaryDirectory()
    db_file = os.path.join(temp_dir.name, "test_admin.db")
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
    return bot


def make_chat_member(status="member"):
    m = MagicMock()
    m.status = status
    return m


def make_group_update(chat_id=100, user_id=1, first_name="User", text="", sender_chat_id=None):
    update = MagicMock(spec=Update)
    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    chat.type = "supergroup"
    update.effective_chat = chat

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = first_name
    user.username = f"user_{user_id}"
    update.effective_user = user

    msg = MagicMock(spec=Message)
    msg.message_id = 555
    msg.text = text
    msg.reply_text = AsyncMock(return_value=MagicMock(message_id=777))
    if sender_chat_id is not None:
        sender_chat = MagicMock(spec=Chat)
        sender_chat.id = sender_chat_id
        msg.sender_chat = sender_chat
    else:
        msg.sender_chat = None

    update.effective_message = msg
    update.callback_query = None
    update.chat_member = None
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
    user.username = f"user_{user_id}"
    update.effective_user = user

    query = MagicMock(spec=CallbackQuery)
    query.data = data
    query.from_user = user
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    query.message.message_id = 444
    query.message.sender_chat = None

    update.callback_query = query
    update.effective_message = query.message
    update.chat_member = None
    return update


# ---------------------------------------------------------------------------
# TEST 1: Group admin who is not in the game can use all controls
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_group_admin_not_in_game_can_use_all_controls(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Room created with 4 players (Host=1, Players=2,3,4)
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    for uid in [2, 3, 4]:
        await service.join_room(chat_id=100, user_id=uid, first_name=f"P{uid}")

    # Admin 99 is NOT in the game
    mock_bot.get_chat_member.return_value = make_chat_member("administrator")

    context = MagicMock()

    # 1. Admin changes round count
    rnd_update = make_callback_update(chat_id=100, user_id=99, first_name="AdminAlice", data="rnd:100:10")
    await handlers.handle_callback_query(rnd_update, context)
    rnd_update.callback_query.answer.assert_called_with("Rounds: 10")
    room = await temp_db.load_room(100)
    assert room.total_rounds == 10
    assert 99 not in [p.user_id for p in room.players]

    # 2. Admin starts match via button
    start_update = make_callback_update(chat_id=100, user_id=99, first_name="AdminAlice", data="start:100")
    await handlers.handle_callback_query(start_update, context)
    room = await temp_db.load_room(100)
    assert room.state == RoomState.PLAYING
    assert 99 not in [p.user_id for p in room.players]

    # 3. Admin closes lobby via /close
    close_update = make_group_update(chat_id=100, user_id=99, first_name="AdminAlice", text="/close")
    await handlers.cmd_close(close_update, context)
    room = await temp_db.load_room(100)
    assert room.is_lobby_locked is True
    close_update.effective_message.reply_text.assert_called_with("🔒 Lobby closed by AdminAlice.")
    assert 99 not in [p.user_id for p in room.players]

    # 4. Admin opens lobby via /open
    open_update = make_group_update(chat_id=100, user_id=99, first_name="AdminAlice", text="/open")
    await handlers.cmd_open(open_update, context)
    room = await temp_db.load_room(100)
    assert room.is_lobby_locked is False
    assert "Opened by AdminAlice" in open_update.effective_message.reply_text.call_args[0][0]
    assert 99 not in [p.user_id for p in room.players]

    # 5. Flag player 2 as inactive, admin removes them via /remove & confirm
    p2 = room.get_player(2)
    p2.is_flagged = True
    await temp_db.save_room(room)

    rm_update = make_group_update(chat_id=100, user_id=99, first_name="AdminAlice", text="/remove")
    await handlers.cmd_remove(rm_update, context)
    assert rm_update.effective_message.reply_text.called

    rmcnf_update = make_callback_update(chat_id=100, user_id=99, first_name="AdminAlice", data="rmcnf:100:2")
    await handlers.handle_callback_query(rmcnf_update, context)
    assert "⚠️ P2 was removed by AdminAlice." in rmcnf_update.callback_query.edit_message_text.call_args[0][0]
    room = await temp_db.load_room(100)
    assert room.get_player(2).is_removed is True
    assert 99 not in [p.user_id for p in room.players]

    # 6. Admin kills game via /kill & confirm
    kill_update = make_group_update(chat_id=100, user_id=99, first_name="AdminAlice", text="/kill")
    await handlers.cmd_kill(kill_update, context)
    assert kill_update.effective_message.reply_text.called

    killcnf_update = make_callback_update(chat_id=100, user_id=99, first_name="AdminAlice", data="killcnf:100")
    await handlers.handle_callback_query(killcnf_update, context)
    edit_text = killcnf_update.callback_query.edit_message_text.call_args[0][0]
    assert "💀 The game was ended by AdminAlice." in edit_text
    room = await temp_db.load_room(100)
    assert room is None  # Deleted cleanly


# ---------------------------------------------------------------------------
# TEST 2: Non-admin, non-host player gets rejection message
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_non_admin_non_host_gets_rejection_message(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Room created with Host=1 and Player=2,3,4
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    for uid in [2, 3, 4]:
        await service.join_room(chat_id=100, user_id=uid, first_name=f"P{uid}")

    # User 50 is a regular member
    mock_bot.get_chat_member.return_value = make_chat_member("member")
    context = MagicMock()

    # /start in group
    u_start = make_group_update(chat_id=100, user_id=50, first_name="RegularBob", text="/start")
    await handlers.cmd_start(u_start, context)
    u_start.effective_message.reply_text.assert_called_with("Only the host or a group admin can do that.")

    # button start
    cb_start = make_callback_update(chat_id=100, user_id=50, first_name="RegularBob", data="start:100")
    await handlers.handle_callback_query(cb_start, context)
    cb_start.callback_query.answer.assert_called_with("Only the host or a group admin can do that.", show_alert=True)

    # button rnd
    cb_rnd = make_callback_update(chat_id=100, user_id=50, first_name="RegularBob", data="rnd:100:8")
    await handlers.handle_callback_query(cb_rnd, context)
    cb_rnd.callback_query.answer.assert_called_with("Only the host or a group admin can do that.", show_alert=True)

    # /open
    u_open = make_group_update(chat_id=100, user_id=50, first_name="RegularBob", text="/open")
    await handlers.cmd_open(u_open, context)
    u_open.effective_message.reply_text.assert_called_with("Only the host or a group admin can do that.")

    # /close
    u_close = make_group_update(chat_id=100, user_id=50, first_name="RegularBob", text="/close")
    await handlers.cmd_close(u_close, context)
    u_close.effective_message.reply_text.assert_called_with("Only the host or a group admin can do that.")

    # /kill
    u_kill = make_group_update(chat_id=100, user_id=50, first_name="RegularBob", text="/kill")
    await handlers.cmd_kill(u_kill, context)
    u_kill.effective_message.reply_text.assert_called_with("Only the host or a group admin can do that.")

    # button killcnf
    cb_kcnf = make_callback_update(chat_id=100, user_id=50, first_name="RegularBob", data="killcnf:100")
    await handlers.handle_callback_query(cb_kcnf, context)
    cb_kcnf.callback_query.answer.assert_called_with("Only the host or a group admin can do that.", show_alert=True)

    # button killcan
    cb_kcan = make_callback_update(chat_id=100, user_id=50, first_name="RegularBob", data="killcan:100")
    await handlers.handle_callback_query(cb_kcan, context)
    cb_kcan.callback_query.answer.assert_called_with("Only the host or a group admin can do that.", show_alert=True)

    # /remove
    u_rm = make_group_update(chat_id=100, user_id=50, first_name="RegularBob", text="/remove")
    await handlers.cmd_remove(u_rm, context)
    u_rm.effective_message.reply_text.assert_called_with("Only the host or a group admin can do that.")

    # button rmsel
    cb_rmsel = make_callback_update(chat_id=100, user_id=50, first_name="RegularBob", data="rmsel:100:2")
    await handlers.handle_callback_query(cb_rmsel, context)
    cb_rmsel.callback_query.answer.assert_called_with("Only the host or a group admin can do that.", show_alert=True)

    # button rmcnf
    cb_rmcnf = make_callback_update(chat_id=100, user_id=50, first_name="RegularBob", data="rmcnf:100:2")
    await handlers.handle_callback_query(cb_rmcnf, context)
    cb_rmcnf.callback_query.answer.assert_called_with("Only the host or a group admin can do that.", show_alert=True)

    # button rmcan
    cb_rmcan = make_callback_update(chat_id=100, user_id=50, first_name="RegularBob", data="rmcan:100")
    await handlers.handle_callback_query(cb_rmcan, context)
    cb_rmcan.callback_query.answer.assert_called_with("Only the host or a group admin can do that.", show_alert=True)


# ---------------------------------------------------------------------------
# TEST 3: Anonymous admin messages are treated as admin
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_anonymous_admin_messages_treated_as_admin(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Create room with 4 players
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    for uid in [2, 3, 4]:
        await service.join_room(chat_id=100, user_id=uid, first_name=f"P{uid}")

    context = MagicMock()

    # Anonymous admin message has sender_chat.id == chat.id (100)
    anon_start = make_group_update(chat_id=100, user_id=1087968824, first_name="Group", text="/start", sender_chat_id=100)
    await handlers.cmd_start(anon_start, context)
    room = await temp_db.load_room(100)
    assert room.state == RoomState.PLAYING

    # Anonymous admin /close
    anon_close = make_group_update(chat_id=100, user_id=1087968824, first_name="Group", text="/close", sender_chat_id=100)
    await handlers.cmd_close(anon_close, context)
    anon_close.effective_message.reply_text.assert_called_with("🔒 Lobby closed by a group admin.")
    room = await temp_db.load_room(100)
    assert room.is_lobby_locked is True

    # Anonymous admin /open
    anon_open = make_group_update(chat_id=100, user_id=1087968824, first_name="Group", text="/open", sender_chat_id=100)
    await handlers.cmd_open(anon_open, context)
    assert "Opened by a group admin" in anon_open.effective_message.reply_text.call_args[0][0]
    room = await temp_db.load_room(100)
    assert room.is_lobby_locked is False


# ---------------------------------------------------------------------------
# TEST 4: User who loses admin rights is blocked immediately (cache TTL & invalidation)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_user_losing_admin_rights_blocked(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # Step 1: User 77 is admin -> verified and cached
    mock_bot.get_chat_member.return_value = make_chat_member("administrator")
    is_admin = await service.is_privileged_user(chat_id=100, user_id=77)
    assert is_admin is True

    # Cache hit check
    assert (100, 77) in service._admin_cache

    # Step 2: User 77 loses admin rights via chat_member update
    mock_bot.get_chat_member.return_value = make_chat_member("member")
    cm_update = MagicMock(spec=Update)
    cm_update.chat_member = MagicMock(spec=ChatMemberUpdated)
    cm_update.chat_member.chat = MagicMock(id=100)
    cm_update.chat_member.new_chat_member = MagicMock()
    cm_update.chat_member.new_chat_member.user = MagicMock(id=77)
    await handlers.handle_chat_member(cm_update, MagicMock())

    # Cache must be evicted
    assert (100, 77) not in service._admin_cache

    # Next check should query get_chat_member and return False immediately
    is_admin_after = await service.is_privileged_user(chat_id=100, user_id=77)
    assert is_admin_after is False

    # Step 3: TTL expiration check (no stale cache beyond 60s)
    service._admin_cache[(100, 88)] = (True, time.time() - 61.0)  # expired cache entry
    mock_bot.get_chat_member.return_value = make_chat_member("member")
    is_admin_expired = await service.is_privileged_user(chat_id=100, user_id=88)
    assert is_admin_expired is False


# ---------------------------------------------------------------------------
# TEST 5: Admin using controls is not added to player list or turn order
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_using_controls_not_added_to_player_list(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # 4 players in room
    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    for uid in [2, 3, 4]:
        await service.join_room(chat_id=100, user_id=uid, first_name=f"P{uid}")

    mock_bot.get_chat_member.return_value = make_chat_member("administrator")
    context = MagicMock()

    # Admin 99 uses start
    cb_start = make_callback_update(chat_id=100, user_id=99, first_name="AdminAlice", data="start:100")
    await handlers.handle_callback_query(cb_start, context)

    room = await temp_db.load_room(100)
    assert len(room.players) == 4
    assert 99 not in [p.user_id for p in room.players]
    assert 99 not in [p.user_id for p in room.active_players]
    assert 99 != room.active_player_id


# ---------------------------------------------------------------------------
# TEST 6: Admins can act while host is inactive or has been removed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_can_act_while_host_inactive_or_removed(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    for uid in [2, 3, 4, 5]:
        await service.join_room(chat_id=100, user_id=uid, first_name=f"P{uid}")

    room = await temp_db.load_room(100)
    # Host is removed
    host = room.get_player(1)
    host.is_removed = True
    await temp_db.save_room(room)

    # Admin 99 can still lock and unlock lobby
    mock_bot.get_chat_member.return_value = make_chat_member("administrator")
    context = MagicMock()

    close_update = make_group_update(chat_id=100, user_id=99, first_name="AdminAlice", text="/close")
    await handlers.cmd_close(close_update, context)
    room = await temp_db.load_room(100)
    assert room.is_lobby_locked is True

    open_update = make_group_update(chat_id=100, user_id=99, first_name="AdminAlice", text="/open")
    await handlers.cmd_open(open_update, context)
    room = await temp_db.load_room(100)
    assert room.is_lobby_locked is False
