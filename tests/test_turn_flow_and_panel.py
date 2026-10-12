import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram import Update, User, Chat, Message, CallbackQuery, InlineQuery, ChosenInlineResult

from fruit_pass.models import Room, RoomState, Player, Card
from fruit_pass.game_service import GameService
from fruit_pass.bot_handlers import BotHandlers
from fruit_pass.database import Database
import fruit_pass.bot_messages as msgs
import fruit_pass.bot_keyboards as kb
import fruit_pass.config as config
from fruit_pass.game_engine import GameEngine


@pytest.fixture
def temp_db():
    temp_dir = tempfile.TemporaryDirectory()
    db_file = os.path.join(temp_dir.name, "test_turn_flow.db")
    db = Database(db_path=db_file)
    yield db
    temp_dir.cleanup()


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=901))
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=901))
    bot.delete_message = AsyncMock(return_value=True)
    bot.get_chat_member = AsyncMock()
    return bot


def make_chat_member(status="member"):
    m = MagicMock()
    m.status = status
    return m


def make_group_update(chat_id=100, user_id=1, first_name="User", last_name=None, text=""):
    update = MagicMock(spec=Update)
    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    chat.type = "supergroup"
    update.effective_chat = chat

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = first_name
    user.last_name = last_name
    user.username = f"user_{user_id}"
    update.effective_user = user

    msg = MagicMock(spec=Message)
    msg.message_id = 555
    msg.text = text
    msg.reply_text = AsyncMock(return_value=MagicMock(message_id=777))
    msg.sender_chat = None

    update.effective_message = msg
    update.callback_query = None
    update.chat_member = None
    return update


def make_callback_update(chat_id=100, user_id=1, first_name="User", last_name=None, data=""):
    update = MagicMock(spec=Update)
    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    chat.type = "supergroup"
    update.effective_chat = chat

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = first_name
    user.last_name = last_name
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
# TEST 1: Manual pass flow: pass announcement then exactly 1 turn panel
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_manual_pass_flow_announcement_and_panel(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # 4 players with first and last names
    p1 = Player(user_id=1, room_id=100, first_name="Alice", last_name="Smith", seat_order=0)
    p2 = Player(user_id=2, room_id=100, first_name="Bob", last_name="Jones", seat_order=1)
    p3 = Player(user_id=3, room_id=100, first_name="Charlie", last_name=None, seat_order=2)
    p4 = Player(user_id=4, room_id=100, first_name="Diana", last_name="Prince", seat_order=3)

    room = Room(chat_id=100, host_id=1, players=[p1, p2, p3, p4])
    GameEngine.deal_round(room, round_number=1)
    await temp_db.save_room(room)

    starter = room.get_active_player()
    receiver = room.next_active_player_after(starter.user_id)
    card = starter.cards[0]

    # Verify pass announcement text format
    announcement = msgs.get_turn_announcement(starter, receiver)
    assert announcement == f"🎴 {starter.display_name} passed a card to {receiver.display_name}"
    assert "@" not in announcement

    # Check inline query results generated for the passer
    iq_update = MagicMock(spec=Update)
    query = AsyncMock(spec=InlineQuery)
    query.from_user = User(id=starter.user_id, first_name=starter.first_name, last_name=starter.last_name, is_bot=False)
    iq_update.inline_query = query
    await handlers.handle_inline_query(iq_update, MagicMock())

    answered_results = query.answer.call_args[1]["results"]
    assert len(answered_results) == len(starter.cards)
    for r in answered_results:
        # Pass announcement is the input_message_content
        assert r.input_message_content.message_text == announcement
        assert r.input_message_content.parse_mode == "HTML"

    # Now execute manual pass
    mock_bot.send_message.reset_mock()
    success, err, p_card, passer, recv = await service.process_pass(
        chat_id=100,
        passer_id=starter.user_id,
        card_id=card.id,
        nonce=room.turn_nonce,
    )
    assert success is True

    # Group gets exactly one turn panel message from the bot
    assert mock_bot.send_message.call_count == 1
    call_args, call_kwargs = mock_bot.send_message.call_args

    panel_text = call_kwargs["text"]
    assert panel_text == f'⏩ Next player: <a href="tg://user?id={receiver.user_id}">{receiver.display_name}</a>'
    assert panel_text.count("\n") == 0  # Exactly one line
    assert call_kwargs["parse_mode"] == "HTML"

    # Exactly one inline keyboard button with "Make your choice!"
    reply_markup = call_kwargs["reply_markup"]
    assert len(reply_markup.inline_keyboard) == 1
    assert len(reply_markup.inline_keyboard[0]) == 1
    btn = reply_markup.inline_keyboard[0][0]
    assert btn.text == "Make your choice!"
    assert btn.switch_inline_query_current_chat == ""


# ---------------------------------------------------------------------------
# TEST 2: Auto-pass flow: auto-pass announcement then exactly 1 turn panel
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_autopass_flow_announcement_and_panel(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)

    p1 = Player(user_id=1, room_id=100, first_name="Alice", last_name="Smith", seat_order=0)
    p2 = Player(user_id=2, room_id=100, first_name="Bob", last_name="Jones", seat_order=1)
    p3 = Player(user_id=3, room_id=100, first_name="Charlie", last_name=None, seat_order=2)
    p4 = Player(user_id=4, room_id=100, first_name="Diana", last_name="Prince", seat_order=3)

    room = Room(chat_id=100, host_id=1, players=[p1, p2, p3, p4])
    GameEngine.deal_round(room, round_number=1)
    await temp_db.save_room(room)

    current_active = room.get_active_player()
    next_recv = room.next_active_player_after(current_active.user_id)

    mock_bot.send_message.reset_mock()
    success, passer, recv = await service.process_autopass(100)
    assert success is True

    # Group gets 1) "⏱ Auto-passed for [Name]." announcement, then 2) turn panel
    assert mock_bot.send_message.call_count == 2
    announcement_call = mock_bot.send_message.call_args_list[0]
    panel_call = mock_bot.send_message.call_args_list[1]

    assert announcement_call.kwargs["text"] == f"⏱ Auto-passed for {current_active.display_name}."

    panel_text = panel_call.kwargs["text"]
    assert panel_text == f'⏩ Next player: <a href="tg://user?id={next_recv.user_id}">{next_recv.display_name}</a>'
    assert panel_text.count("\n") == 0
    assert panel_call.kwargs["parse_mode"] == "HTML"

    reply_markup = panel_call.kwargs["reply_markup"]
    assert len(reply_markup.inline_keyboard) == 1
    assert len(reply_markup.inline_keyboard[0]) == 1
    assert reply_markup.inline_keyboard[0][0].text == "Make your choice!"
    assert reply_markup.inline_keyboard[0][0].switch_inline_query_current_chat == ""


# ---------------------------------------------------------------------------
# TEST 3: Mentions use display names, work without usernames, send notifications
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mention_uses_display_name_and_notifies_without_username():
    # User with username
    p1 = Player(user_id=10, room_id=1, first_name="Alice", last_name="Walker", username="alicew")
    mention1 = msgs.get_player_mention(p1)
    assert mention1 == '<a href="tg://user?id=10">Alice Walker</a>'
    assert "@alicew" not in mention1

    # User without username
    p2 = Player(user_id=20, room_id=1, first_name="Bob", last_name=None, username=None)
    mention2 = msgs.get_player_mention(p2)
    assert mention2 == '<a href="tg://user?id=20">Bob</a>'

    # User with special HTML chars in first and last name
    p3 = Player(user_id=30, room_id=1, first_name="Eve <Hacker>", last_name="A & B", username="evehack")
    mention3 = msgs.get_player_mention(p3)
    assert mention3 == '<a href="tg://user?id=30">Eve &lt;Hacker&gt; A &amp; B</a>'
    assert "@evehack" not in mention3

    # Turn panel text test
    assert msgs.get_turn_panel_text(p1) == '⏩ Next player: <a href="tg://user?id=10">Alice Walker</a>'
    assert msgs.get_turn_panel_text(p2) == '⏩ Next player: <a href="tg://user?id=20">Bob</a>'


# ---------------------------------------------------------------------------
# TEST 4: CLEANUP_ENABLED off preserves earlier messages and buttons
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cleanup_disabled_by_default_preserves_earlier_messages(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    assert config.CLEANUP_ENABLED is False

    p1 = Player(user_id=1, room_id=100, first_name="Alice", seat_order=0)
    p2 = Player(user_id=2, room_id=100, first_name="Bob", seat_order=1)
    p3 = Player(user_id=3, room_id=100, first_name="Charlie", seat_order=2)
    p4 = Player(user_id=4, room_id=100, first_name="Diana", seat_order=3)

    room = Room(chat_id=100, host_id=1, players=[p1, p2, p3, p4])
    GameEngine.deal_round(room, round_number=1)
    await temp_db.save_room(room)

    # 1. First manual pass
    card1 = room.get_active_player().cards[0]
    await service.process_pass(100, passer_id=1, card_id=card1.id, nonce=room.turn_nonce)

    # 2. Second manual pass
    room = await temp_db.load_room(100)
    active2 = room.get_active_player()
    card2 = active2.cards[0]
    await service.process_pass(100, passer_id=active2.user_id, card_id=card2.id, nonce=room.turn_nonce)

    # 3. Third pass via autopass
    await service.process_autopass(100)

    # Verify delete_message was NEVER called
    mock_bot.delete_message.assert_not_called()

    # Now verify old buttons still open a correct inline list based on current state:
    room = await temp_db.load_room(100)
    current_active_id = room.active_player_id

    # Non-active player taps button: receives view-only list
    non_active_id = 1 if current_active_id != 1 else 2
    iq_non_active = MagicMock(spec=Update)
    q_non = AsyncMock(spec=InlineQuery)
    q_non.from_user = User(id=non_active_id, first_name=f"P{non_active_id}", is_bot=False)
    iq_non_active.inline_query = q_non
    await handlers.handle_inline_query(iq_non_active, MagicMock())

    results_non = q_non.answer.call_args[1]["results"]
    for r in results_non:
        assert r.description == "Not your turn."

    # Active player taps button: receives pass list
    iq_active = MagicMock(spec=Update)
    q_act = AsyncMock(spec=InlineQuery)
    q_act.from_user = User(id=current_active_id, first_name=f"P{current_active_id}", is_bot=False)
    iq_active.inline_query = q_act
    await handlers.handle_inline_query(iq_active, MagicMock())

    results_act = q_act.answer.call_args[1]["results"]
    for r in results_act:
        assert r.description.startswith("Tap to pass this card")


# ---------------------------------------------------------------------------
# TEST 5: Round 1 and Round N+1 starts end with the same panel for starter
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_round_start_ends_with_turn_panel_for_starter(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)

    p1 = Player(user_id=1, room_id=100, first_name="Alice", last_name="Wonder", seat_order=0)
    p2 = Player(user_id=2, room_id=100, first_name="Bob", last_name="Builder", seat_order=1)
    p3 = Player(user_id=3, room_id=100, first_name="Charlie", last_name="Bucket", seat_order=2)
    p4 = Player(user_id=4, room_id=100, first_name="Diana", last_name="Prince", seat_order=3)

    room = Room(chat_id=100, host_id=1, current_round=1, players=[p1, p2, p3, p4])
    starter, newly_seated = GameEngine.deal_round(room, round_number=1)
    await temp_db.save_room(room)

    # --- Round 1 start ---
    mock_bot.send_message.reset_mock()
    await service.send_round_transition_messages(room, starter, newly_seated)

    # Messages sent: shuffle, pass order, "✅ Round 1 has now started!", turn panel
    calls_r1 = mock_bot.send_message.call_args_list
    assert len(calls_r1) == 4
    assert "🔀 Shuffling players for Round 1..." in calls_r1[0].kwargs["text"]
    assert "🎲 New pass order:" in calls_r1[1].kwargs["text"]
    # Ended sequence with "✅ Round 1 has now started!" (no tag)
    assert calls_r1[2].kwargs["text"] == "✅ Round 1 has now started!"
    assert "@" not in calls_r1[2].kwargs["text"]
    # Followed by turn panel for starter
    assert calls_r1[3].kwargs["text"] == f'⏩ Next player: <a href="tg://user?id={starter.user_id}">{starter.display_name}</a>'
    assert calls_r1[3].kwargs["reply_markup"].inline_keyboard[0][0].text == "Make your choice!"

    # --- Round 2 (Round N+1) start ---
    room.current_round = 2
    win_msg = f"🏆 {p1.display_name} wins Round 1 with four 🍎 Apples!"
    starter2, newly_seated2 = GameEngine.deal_round(room, round_number=2)
    await temp_db.save_room(room)

    mock_bot.send_message.reset_mock()
    await service.send_round_transition_messages(room, starter2, newly_seated2, round_win_msg=win_msg)

    calls_r2 = mock_bot.send_message.call_args_list
    assert len(calls_r2) == 5
    assert calls_r2[0].kwargs["text"] == win_msg
    assert "🔀 Shuffling players for Round 2..." in calls_r2[1].kwargs["text"]
    assert "🎲 New pass order:" in calls_r2[2].kwargs["text"]
    # Ended sequence with "✅ Round 2 has now started!" (no tag)
    assert calls_r2[3].kwargs["text"] == "✅ Round 2 has now started!"
    assert "@" not in calls_r2[3].kwargs["text"]
    # Followed by the same turn panel for starter2
    assert calls_r2[4].kwargs["text"] == f'⏩ Next player: <a href="tg://user?id={starter2.user_id}">{starter2.display_name}</a>'
    assert calls_r2[4].kwargs["reply_markup"].inline_keyboard[0][0].text == "Make your choice!"


# ---------------------------------------------------------------------------
# TEST 6: Host who is not a group admin can use every control
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_host_not_group_admin_can_use_every_control(temp_db, mock_bot):
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)

    # Host is user_id 1. Telegram get_chat_member says host is a regular "member" (NOT admin)
    mock_bot.get_chat_member.return_value = make_chat_member("member")

    await service.create_room(chat_id=100, host_id=1, host_first_name="HostAlice")
    for uid in [2, 3, 4]:
        await service.join_room(chat_id=100, user_id=uid, first_name=f"P{uid}")

    context = MagicMock()

    # 1. Host changes round count via button
    rnd_cb = make_callback_update(chat_id=100, user_id=1, first_name="HostAlice", data="rnd:100:10")
    await handlers.handle_callback_query(rnd_cb, context)
    rnd_cb.callback_query.answer.assert_called_with("Rounds: 10")
    room = await temp_db.load_room(100)
    assert room.total_rounds == 10

    # 2. Host starts match via /start in group
    start_cmd = make_group_update(chat_id=100, user_id=1, first_name="HostAlice", text="/start")
    await handlers.cmd_start(start_cmd, context)
    room = await temp_db.load_room(100)
    assert room.state == RoomState.PLAYING

    # 3. Host closes lobby via /close
    close_cmd = make_group_update(chat_id=100, user_id=1, first_name="HostAlice", text="/close")
    await handlers.cmd_close(close_cmd, context)
    room = await temp_db.load_room(100)
    assert room.is_lobby_locked is True

    # 4. Host opens lobby via /open
    open_cmd = make_group_update(chat_id=100, user_id=1, first_name="HostAlice", text="/open")
    await handlers.cmd_open(open_cmd, context)
    room = await temp_db.load_room(100)
    assert room.is_lobby_locked is False

    # 5. Host removes inactive player via /remove and confirmation
    p2 = room.get_player(2)
    p2.is_flagged = True
    await temp_db.save_room(room)

    rm_cmd = make_group_update(chat_id=100, user_id=1, first_name="HostAlice", text="/remove")
    await handlers.cmd_remove(rm_cmd, context)
    assert rm_cmd.effective_message.reply_text.called

    rmcnf_cb = make_callback_update(chat_id=100, user_id=1, first_name="HostAlice", data="rmcnf:100:2")
    await handlers.handle_callback_query(rmcnf_cb, context)
    room = await temp_db.load_room(100)
    assert room.get_player(2).is_removed is True

    # 6. Host kills game via /kill and confirmation
    kill_cmd = make_group_update(chat_id=100, user_id=1, first_name="HostAlice", text="/kill")
    await handlers.cmd_kill(kill_cmd, context)
    assert kill_cmd.effective_message.reply_text.called

    killcnf_cb = make_callback_update(chat_id=100, user_id=1, first_name="HostAlice", data="killcnf:100")
    await handlers.handle_callback_query(killcnf_cb, context)
    room = await temp_db.load_room(100)
    assert room is None  # Ended cleanly


# ---------------------------------------------------------------------------
# TEST 7: Panel text never contains card, fruit, timer, or hand information
# ---------------------------------------------------------------------------
def test_no_panel_text_contains_card_fruit_or_hand_information():
    for first in ["Mia", "Leo", "John", "Alice"]:
        for last in [None, "Doe", "Smith"]:
            p = Player(user_id=99, room_id=1, first_name=first, last_name=last)
            panel = msgs.get_turn_panel_text(p)

            # Exactly one line
            assert "\n" not in panel
            assert panel.startswith("⏩ Next player: ")

            # No card or fruit info
            for _, name, emoji in config.FRUIT_RANKS:
                assert name.lower() not in panel.lower()
                assert emoji not in panel

            # No timer information
            assert "60" not in panel
            assert "second" not in panel.lower()
            assert "timer" not in panel.lower()

            # No hand information
            assert "card" not in panel.lower()
            assert "hand" not in panel.lower()
            assert "kind" not in panel.lower()
