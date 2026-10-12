import asyncio
import html
import pytest
from unittest.mock import AsyncMock, MagicMock

from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.game_engine import GameEngine
from fruit_pass.database import Database
from fruit_pass.game_service import GameService
from fruit_pass.bot_handlers import BotHandlers
import fruit_pass.bot_messages as msgs


import tempfile
import os


@pytest.fixture
def memory_db():
    temp_dir = tempfile.TemporaryDirectory()
    db_file = os.path.join(temp_dir.name, "test_features.db")
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
# 1. TURN PANEL AFTER EVERY PASS
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_turn_ping_message_format():
    # User with username - must use display name mention, not @username
    p1 = Player(user_id=10, room_id=1, first_name="Alice", username="alice_in_wonderland")
    msg1 = msgs.get_turn_panel_text(p1)
    assert msg1 == '⏩ Next player: <a href="tg://user?id=10">Alice</a>'
    assert "@alice_in_wonderland" not in msg1
    assert "Apple" not in msg1 and "Mango" not in msg1

    # User without username (HTML link & escaped name)
    p2 = Player(user_id=20, room_id=1, first_name="Bob <Cool> & Fun", username=None)
    msg2 = msgs.get_turn_panel_text(p2)
    assert msg2 == '⏩ Next player: <a href="tg://user?id=20">Bob &lt;Cool&gt; &amp; Fun</a>'


@pytest.mark.asyncio
async def test_turn_ping_sent_on_manual_and_autopass(memory_db, mock_bot):
    await memory_db.init_db()
    service = GameService(db=memory_db, bot=mock_bot)

    room = Room(
        chat_id=100,
        host_id=1,
        players=[
            Player(user_id=1, room_id=100, first_name="Alice", username="alice", seat_order=0),
            Player(user_id=2, room_id=100, first_name="Bob", username=None, seat_order=1),
            Player(user_id=3, room_id=100, first_name="Charlie", username="charlie", seat_order=2),
            Player(user_id=4, room_id=100, first_name="Diana", username="diana", seat_order=3),
        ],
    )
    # Start match
    GameEngine.deal_round(room, round_number=1)
    await memory_db.save_room(room)

    # 1. Manual pass: starter passes to next
    starter = room.get_active_player()
    receiver = room.next_active_player_after(starter.user_id)
    card = starter.cards[0]

    mock_bot.send_message.reset_mock()
    mock_bot.delete_message.reset_mock()

    success, err, p_card, p_user, r_user = await service.process_pass(
        chat_id=100,
        passer_id=starter.user_id,
        card_id=card.id,
        nonce=room.turn_nonce,
    )
    assert success is True
    # Verify bot sent turn panel tagging receiver
    assert mock_bot.send_message.called
    sent_args, sent_kwargs = mock_bot.send_message.call_args
    assert sent_kwargs["parse_mode"] == "HTML"
    assert f'tg://user?id={receiver.user_id}' in sent_kwargs["text"]
    assert f">{receiver.display_name}</a>" in sent_kwargs["text"]
    assert sent_kwargs["reply_markup"].inline_keyboard[0][0].text == "Make your choice!"

    # Verify CLEANUP_ENABLED is False by default: no deletion
    mock_bot.delete_message.assert_not_called()

    # Verify room updated with last_ping_message_id
    room = await memory_db.load_room(100)
    assert room.last_ping_message_id == 999

    # 2. Auto-pass: next player turn expires
    current_active = room.get_active_player()
    next_recv = room.next_active_player_after(current_active.user_id)

    mock_bot.send_message.reset_mock()
    mock_bot.delete_message.reset_mock()

    succ_auto, passer_auto, recv_auto = await service.process_autopass(100)
    assert succ_auto is True
    # Verify CLEANUP_ENABLED off -> no deletion
    mock_bot.delete_message.assert_not_called()
    # Verify messages sent: 1) auto-pass announcement, 2) turn panel
    assert mock_bot.send_message.call_count == 2
    auto_announcement_call = mock_bot.send_message.call_args_list[0]
    turn_panel_call = mock_bot.send_message.call_args_list[1]
    assert f"⏱ Auto-passed for {current_active.display_name}." in auto_announcement_call.kwargs["text"]
    assert f'tg://user?id={next_recv.user_id}' in turn_panel_call.kwargs["text"]
    assert turn_panel_call.kwargs["reply_markup"].inline_keyboard[0][0].text == "Make your choice!"


# -------------------------------------------------------------
# 2. FAIR STARTER CYCLE AND RANDOM SHUFFLE
# -------------------------------------------------------------
def test_fair_starter_cycle_and_reset():
    for num_players in (4, 6, 8):
        for total_rounds in (5, 8, 10):
            players = [
                Player(user_id=i, room_id=10, first_name=f"P{i}", seat_order=i - 1)
                for i in range(1, num_players + 1)
            ]
            room = Room(chat_id=10, host_id=1, total_rounds=total_rounds, players=players)

            seen_starters_in_current_cycle = []
            for r in range(1, total_rounds + 1):
                starter, _ = GameEngine.deal_round(room, round_number=r)
                assert starter.user_id in [p.user_id for p in room.active_players]

                # Verify starter was not already started in this cycle
                assert starter.user_id not in room.started_this_cycle
                seen_starters_in_current_cycle.append(starter.user_id)

                # Conclude round (simulate win)
                GameEngine.conclude_round(room, winner_id=starter.user_id)

                if len(seen_starters_in_current_cycle) == num_players:
                    # Full cycle completed
                    assert len(room.started_this_cycle) == 0  # Should have reset
                    seen_starters_in_current_cycle = []


def test_starter_cycle_avoids_previous_starter_on_reset():
    # Test cycle reset with 4 players: in the new cycle, the previous round's starter should not repeat
    for _ in range(20):
        players = [
            Player(user_id=i, room_id=10, first_name=f"P{i}", seat_order=i - 1)
            for i in range(1, 5)
        ]
        room = Room(chat_id=10, host_id=1, players=players)

        # 4 rounds to complete cycle 1
        last_starter_c1 = None
        for r in range(1, 5):
            starter, _ = GameEngine.deal_round(room, round_number=r)
            last_starter_c1 = starter.user_id
            GameEngine.conclude_round(room, winner_id=starter.user_id)

        assert len(room.started_this_cycle) == 0  # reset

        # Round 5 starts cycle 2
        starter_c2, _ = GameEngine.deal_round(room, round_number=5)
        assert starter_c2.user_id != last_starter_c1


def test_voided_round_does_not_consume_starter_slot():
    players = [
        Player(user_id=i, room_id=10, first_name=f"P{i}", seat_order=i - 1)
        for i in range(1, 5)
    ]
    room = Room(chat_id=10, host_id=1, players=players)

    starter1, _ = GameEngine.deal_round(room, round_number=1)
    # Round 1 is in progress. Suppose player 4 is removed (voiding round 1)
    round_voided, can_cont = GameEngine.remove_player_and_handle_state(room, 4)
    assert round_voided is True

    # Starter slot for starter1 must NOT be consumed in started_this_cycle
    assert starter1.user_id not in room.started_this_cycle


def test_removed_player_leaves_starter_pool():
    players = [
        Player(user_id=i, room_id=10, first_name=f"P{i}", seat_order=i - 1)
        for i in range(1, 6)
    ]
    room = Room(chat_id=10, host_id=1, players=players)
    room.started_this_cycle.add(5)

    # Remove player 5
    GameEngine.remove_player_and_handle_state(room, 5)
    assert 5 not in room.started_this_cycle
    assert all(p.user_id != 5 for p in room.active_players)


# -------------------------------------------------------------
# 3. ROUND TRANSITIONS & PASS ORDER SHUFFLE
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_round_transition_messages_sequence(mock_bot):
    room = Room(
        chat_id=555,
        host_id=1,
        current_round=2,
        players=[
            Player(user_id=1, room_id=555, first_name="Alice", username="alice", seat_order=0),
            Player(user_id=2, room_id=555, first_name="Bob", username=None, seat_order=1),
            Player(user_id=3, room_id=555, first_name="Charlie", username="charlie", seat_order=2),
            Player(user_id=4, room_id=555, first_name="Diana", username="diana", seat_order=3),
        ],
    )
    starter = room.players[0]
    newly_seated = [room.players[1]]

    service = GameService(db=None, bot=mock_bot)
    win_msg = "🏆 Alice wins Round 1 with four 🍎 Apples!"

    await service.send_round_transition_messages(
        room=room,
        starter=starter,
        newly_seated=newly_seated,
        round_win_msg=win_msg,
    )

    # Verify calls
    calls = mock_bot.send_message.call_args_list
    assert len(calls) == 6
    # a) win announcement
    assert calls[0].kwargs["text"] == win_msg
    # b) shuffle message
    assert "🔀 Shuffling players for Round 2..." in calls[1].kwargs["text"]
    # c) newly seated
    assert "➕ Bob joins from this round." in calls[2].kwargs["text"]
    # d) pass order (no pings)
    assert "🎲 New pass order:" in calls[3].kwargs["text"]
    assert "@" not in calls[3].kwargs["text"]
    # e) round started (no tag)
    assert calls[4].kwargs["text"] == "✅ Round 2 has now started!"
    # f) turn panel for starter
    assert "⏩ Next player:" in calls[5].kwargs["text"]
    assert f'tg://user?id={starter.user_id}' in calls[5].kwargs["text"]
    assert calls[5].kwargs["reply_markup"].inline_keyboard[0][0].text == "Make your choice!"


# -------------------------------------------------------------
# 4. LOBBY COMMANDS: /open, /close, /kill, NON-HOST CHECKS
# -------------------------------------------------------------
@pytest.mark.asyncio
async def test_lobby_open_and_close(memory_db, mock_bot):
    await memory_db.init_db()
    service = GameService(db=memory_db, bot=mock_bot)

    room, _ = await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    assert room.is_lobby_locked is False

    # Non-host cannot /close
    ok, msg, _ = await service.set_lobby_lock(chat_id=100, requester_id=999, lock=True)
    assert ok is False
    assert msg == "Only the host or a group admin can do that."

    # Host closes lobby
    ok, msg, room = await service.set_lobby_lock(chat_id=100, requester_id=1, lock=True)
    assert ok is True
    assert room.is_lobby_locked is True
    assert "🔒 Lobby closed" in msg

    # Join while closed must be rejected
    join_room, join_msg = await service.join_room(100, user_id=2, first_name="Player2")
    assert join_room is None
    assert "🔒 The lobby is closed." in join_msg

    # Host opens lobby
    ok, msg, room = await service.set_lobby_lock(chat_id=100, requester_id=1, lock=False)
    assert ok is True
    assert room.is_lobby_locked is False
    assert "🔓 Lobby open!" in msg

    # Join while open works
    join_room, join_msg = await service.join_room(100, user_id=2, first_name="Player2")
    assert join_room is not None
    assert join_msg == "Joined successfully."


@pytest.mark.asyncio
async def test_lobby_open_rejects_full_room(memory_db, mock_bot):
    await memory_db.init_db()
    service = GameService(db=memory_db, bot=mock_bot)
    room, _ = await service.create_room(chat_id=100, host_id=1, host_first_name="Host")

    # Add up to 8 players
    for i in range(2, 9):
        await service.join_room(100, user_id=i, first_name=f"P{i}")

    loaded = await memory_db.load_room(100)
    assert len(loaded.all_current_players) == 8

    # Host tries to /open when already full
    ok, msg, _ = await service.set_lobby_lock(chat_id=100, requester_id=1, lock=False)
    assert ok is False
    assert "Lobby is full (8/8)." in msg


@pytest.mark.asyncio
async def test_mid_match_join_and_pause_resumption(memory_db, mock_bot):
    await memory_db.init_db()
    service = GameService(db=memory_db, bot=mock_bot)
    room, _ = await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    for i in range(2, 5):
        await service.join_room(100, user_id=i, first_name=f"P{i}")

    # Start match
    await service.start_match(chat_id=100, requester_id=1)
    loaded = await memory_db.load_room(100)
    assert loaded.state == RoomState.PLAYING

    # Player 5 joins mid-match
    join_room, join_msg = await service.join_room(100, user_id=5, first_name="P5")
    assert join_room is not None
    assert "seated at the start of next round" in join_msg

    p5 = join_room.get_player(5)
    assert p5.is_waiting is True

    # Advance to next round: Player 5 must be seated
    next_room, starter, newly_seated = await service.advance_to_next_round(100)
    p5_after = next_room.get_player(5)
    assert p5_after.is_waiting is False
    assert any(p.user_id == 5 for p in newly_seated)
    assert len(next_room.active_players) == 5
    assert len(p5_after.cards) == 4

    # Test paused match resumption:
    # Flag and remove 2 players so only 3 remain
    p4 = next_room.get_player(4)
    p4.is_flagged = True
    p5_after.is_flagged = True
    await memory_db.save_room(next_room)

    await service.remove_player(100, host_id=1, target_user_id=4)
    await service.remove_player(100, host_id=1, target_user_id=5)

    paused_room = await memory_db.load_room(100)
    assert paused_room.state == RoomState.PAUSED

    # Joiner 6 joins paused room (< 4 players)
    j_room, j_msg = await service.join_room(100, user_id=6, first_name="P6")
    assert j_room.state == RoomState.PLAYING
    assert "Match resumed with fresh redeal" in j_msg


@pytest.mark.asyncio
async def test_kill_command(memory_db, mock_bot):
    await memory_db.init_db()
    service = GameService(db=memory_db, bot=mock_bot)
    room, _ = await service.create_room(chat_id=100, host_id=1, host_first_name="Host")
    await service.join_room(100, user_id=2, first_name="Player2")

    # Non-host cannot /kill
    ok, msg, _ = await service.kill_room(chat_id=100, requester_id=2)
    assert ok is False
    assert msg == "Only the host or a group admin can do that."

    # Host kills room
    ok, msg, scores_str = await service.kill_room(chat_id=100, requester_id=1)
    assert ok is True
    assert msg == "Game ended."
    assert "Host" in scores_str
    assert "Player2" in scores_str

    # Room is deleted
    assert await memory_db.load_room(100) is None
    # Player locks freed: player 1 and 2 can immediately create/join rooms
    assert await memory_db.get_active_room_for_user(1) is None
    assert await memory_db.get_active_room_for_user(2) is None
