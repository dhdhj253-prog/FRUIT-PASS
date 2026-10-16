import asyncio
import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, User, InlineQuery, Bot

from fruit_pass.models import Room, RoomState, Player, Card
from fruit_pass.database import Database
from fruit_pass.game_service import GameService
from fruit_pass.game_engine import GameEngine
from fruit_pass.bot_handlers import BotHandlers
import fruit_pass.bot_messages as msgs
import fruit_pass.bot_keyboards as kb


@pytest.fixture
def temp_db():
    temp_dir = tempfile.TemporaryDirectory()
    db_path = os.path.join(temp_dir.name, "test_autowin.db")
    db = Database(db_path=db_path)
    yield db
    temp_dir.cleanup()


@pytest.fixture
def mock_bot():
    bot = AsyncMock(spec=Bot)
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=901))
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=901))
    bot.delete_message = AsyncMock(return_value=True)
    return bot


@pytest.mark.asyncio
async def test_player_with_4_matching_plus_1_extra_passes_extra_wins_immediately_with_no_claim(temp_db, mock_bot):
    """
    A player with 4 matching plus 1 extra who passes the extra wins immediately with no claim.
    """
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    chat_id = -100101
    p1 = Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0)
    p2 = Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1)
    p3 = Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2)
    p4 = Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3)

    # Alice has 4 Apples (rank 0) and 1 Mango (rank 1)
    p1.cards = [
        Card.create(0, 0),
        Card.create(0, 1),
        Card.create(0, 2),
        Card.create(0, 3),
        Card.create(1, 0),
    ]
    p2.cards = [Card.create(2, 0), Card.create(2, 1), Card.create(3, 0), Card.create(3, 1)]
    p3.cards = [Card.create(4, 0), Card.create(4, 1), Card.create(5, 0), Card.create(5, 1)]
    p4.cards = [Card.create(6, 0), Card.create(6, 1), Card.create(7, 0), Card.create(7, 1)]

    room = Room(
        chat_id=chat_id,
        host_id=1,
        state=RoomState.PLAYING,
        total_rounds=1,  # Final round to inspect final state & cards directly
        current_round=1,
        active_player_id=1,
        turn_nonce=1,
        players=[p1, p2, p3, p4],
    )
    await temp_db.save_room(room)

    # Alice passes the Mango (card_id "c_1_0") to Bob
    success, err, p_card, passer, recv = await service.process_pass(
        chat_id=chat_id,
        passer_id=1,
        card_id="c_1_0",
        nonce=1,
    )
    assert success is True

    # Alice has exactly 4 cards remaining, all Apples
    refreshed_room = await temp_db.load_room(chat_id)
    alice = refreshed_room.get_player(1)
    assert len(alice.cards) == 4
    assert all(c.rank == 0 for c in alice.cards)
    # Alice immediately awarded 1 point and won the round with no claim needed
    assert alice.score == 1
    # Round / match ended immediately
    assert refreshed_room.state == RoomState.MATCH_OVER


@pytest.mark.asyncio
async def test_player1_passes_and_wins_even_when_passed_card_gives_player2_four_matching(temp_db, mock_bot):
    """
    Player 1 passes and wins even when the passed card gives Player 2 four matching cards; Player 2 does not win.
    """
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    chat_id = -100102
    p1 = Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0)
    p2 = Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1)
    p3 = Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2)
    p4 = Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3)

    # Player 1 has 4 Apples and 1 Mango
    p1.cards = [
        Card.create(0, 0),
        Card.create(0, 1),
        Card.create(0, 2),
        Card.create(0, 3),
        Card.create(1, 0),
    ]
    # Player 2 has 3 Mangos and 1 Grape
    p2.cards = [
        Card.create(1, 1),
        Card.create(1, 2),
        Card.create(1, 3),
        Card.create(2, 0),
    ]
    p3.cards = [Card.create(4, 0), Card.create(4, 1), Card.create(5, 0), Card.create(5, 1)]
    p4.cards = [Card.create(6, 0), Card.create(6, 1), Card.create(7, 0), Card.create(7, 1)]

    room = Room(
        chat_id=chat_id,
        host_id=1,
        state=RoomState.PLAYING,
        total_rounds=1,
        current_round=1,
        active_player_id=1,
        turn_nonce=1,
        players=[p1, p2, p3, p4],
    )
    await temp_db.save_room(room)

    # Player 1 passes Mango to Player 2
    success, err, p_card, passer, recv = await service.process_pass(
        chat_id=chat_id,
        passer_id=1,
        card_id="c_1_0",
        nonce=1,
    )
    assert success is True

    refreshed_room = await temp_db.load_room(chat_id)
    alice = refreshed_room.get_player(1)
    bob = refreshed_room.get_player(2)

    # Alice has 4 Apples and won
    assert alice.score == 1
    # Bob received the 4th Mango (now has 4 Mangos + 1 Grape = 5 cards), but did NOT win
    assert bob.score == 0
    assert len(bob.cards) == 5
    assert sum(1 for c in bob.cards if c.rank == 1) == 4
    # Round is over, won by Alice
    assert refreshed_room.state == RoomState.MATCH_OVER


@pytest.mark.asyncio
async def test_receiving_fourth_matching_card_alone_does_not_end_round_receiver_wins_only_after_passing_extra(temp_db, mock_bot):
    """
    Receiving a 4th matching card alone does not end the round; the receiver wins only after passing the extra.
    """
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    chat_id = -100103
    p1 = Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0)
    p2 = Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1)
    p3 = Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2)
    p4 = Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3)

    # Alice has 5 cards, but NOT 4 of a kind (3 Apples, 1 Lemon, 1 Mango)
    p1.cards = [
        Card.create(0, 0),
        Card.create(0, 1),
        Card.create(0, 2),
        Card.create(4, 0),  # Lemon
        Card.create(1, 0),  # Mango
    ]
    # Bob has 3 Mangos and 1 Grape
    p2.cards = [
        Card.create(1, 1),
        Card.create(1, 2),
        Card.create(1, 3),
        Card.create(2, 0),  # Grape
    ]
    p3.cards = [Card.create(4, 1), Card.create(4, 2), Card.create(5, 0), Card.create(5, 1)]
    p4.cards = [Card.create(6, 0), Card.create(6, 1), Card.create(7, 0), Card.create(7, 1)]

    room = Room(
        chat_id=chat_id,
        host_id=1,
        state=RoomState.PLAYING,
        total_rounds=1,
        current_round=1,
        active_player_id=1,
        turn_nonce=1,
        players=[p1, p2, p3, p4],
    )
    await temp_db.save_room(room)

    # Alice passes Mango to Bob
    success, err, p_card, passer, recv = await service.process_pass(
        chat_id=chat_id,
        passer_id=1,
        card_id="c_1_0",
        nonce=1,
    )
    assert success is True

    # Check state after Alice's pass:
    room_mid = await temp_db.load_room(chat_id)
    alice_mid = room_mid.get_player(1)
    bob_mid = room_mid.get_player(2)

    # Alice did not win (has 3 Apples, 1 Lemon)
    assert alice_mid.score == 0
    # Bob received the 4th Mango (holds 4 Mangos + 1 Grape = 5 cards), but has NOT won
    assert bob_mid.score == 0
    assert len(bob_mid.cards) == 5
    assert sum(1 for c in bob_mid.cards if c.rank == 1) == 4
    # Round is STILL playing!
    assert room_mid.state == RoomState.PLAYING
    assert room_mid.active_player_id == 2

    # Now Bob takes his turn and passes the extra Grape ("c_2_0") to Charlie
    success2, err2, p_card2, passer2, recv2 = await service.process_pass(
        chat_id=chat_id,
        passer_id=2,
        card_id="c_2_0",
        nonce=room_mid.turn_nonce,
    )
    assert success2 is True

    # Now Bob has passed, leaving him with exactly 4 matching Mangos -> Bob wins!
    room_final = await temp_db.load_room(chat_id)
    bob_final = room_final.get_player(2)
    assert len(bob_final.cards) == 4
    assert all(c.rank == 1 for c in bob_final.cards)
    assert bob_final.score == 1
    assert room_final.state == RoomState.MATCH_OVER


@pytest.mark.asyncio
async def test_autopass_and_skip_trigger_same_win_check(temp_db, mock_bot):
    """
    Auto-pass and /skip trigger the same win check.
    """
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    chat_id = -100104

    # 1. Test Auto-pass win
    p1 = Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0)
    p2 = Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1)
    p3 = Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2)
    p4 = Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3)

    # Alice has 4 Apples and 1 Mango
    p1.cards = [
        Card.create(0, 0),
        Card.create(0, 1),
        Card.create(0, 2),
        Card.create(0, 3),
        Card.create(1, 0),
    ]
    p2.cards = [Card.create(2, 0), Card.create(2, 1), Card.create(3, 0), Card.create(3, 1)]
    p3.cards = [Card.create(4, 0), Card.create(4, 1), Card.create(5, 0), Card.create(5, 1)]
    p4.cards = [Card.create(6, 0), Card.create(6, 1), Card.create(7, 0), Card.create(7, 1)]

    room = Room(
        chat_id=chat_id,
        host_id=1,
        state=RoomState.PLAYING,
        total_rounds=1,
        current_round=1,
        active_player_id=1,
        turn_nonce=1,
        turn_deadline=1.0,
        players=[p1, p2, p3, p4],
    )
    await temp_db.save_room(room)

    with patch("random.choice", return_value=p1.cards[4]):
        success, passer, recv = await service.process_autopass(chat_id)

    assert success is True
    refreshed = await temp_db.load_room(chat_id)
    alice = refreshed.get_player(1)
    assert alice.score == 1
    assert refreshed.state == RoomState.MATCH_OVER

    # 2. Test /skip win
    chat_id_skip = -100105
    p1_s = Player(user_id=1, room_id=chat_id_skip, first_name="Alice", seat_order=0)
    p2_s = Player(user_id=2, room_id=chat_id_skip, first_name="Bob", seat_order=1)
    p3_s = Player(user_id=3, room_id=chat_id_skip, first_name="Charlie", seat_order=2)
    p4_s = Player(user_id=4, room_id=chat_id_skip, first_name="Diana", seat_order=3)

    p1_s.cards = [
        Card.create(0, 0),
        Card.create(0, 1),
        Card.create(0, 2),
        Card.create(0, 3),
        Card.create(1, 0),
    ]
    p2_s.cards = [Card.create(2, 0), Card.create(2, 1), Card.create(3, 0), Card.create(3, 1)]
    p3_s.cards = [Card.create(4, 0), Card.create(4, 1), Card.create(5, 0), Card.create(5, 1)]
    p4_s.cards = [Card.create(6, 0), Card.create(6, 1), Card.create(7, 0), Card.create(7, 1)]

    room_skip = Room(
        chat_id=chat_id_skip,
        host_id=1,
        state=RoomState.PLAYING,
        total_rounds=1,
        current_round=1,
        active_player_id=1,
        turn_nonce=1,
        players=[p1_s, p2_s, p3_s, p4_s],
    )
    await temp_db.save_room(room_skip)

    with patch("random.choice", return_value=p1_s.cards[4]):
        success_skip, msg_skip, passer_s, recv_s = await service.process_skip(
            chat_id=chat_id_skip,
            requester_id=1,
            actor_name="Host",
        )

    assert success_skip is True
    refreshed_skip = await temp_db.load_room(chat_id_skip)
    alice_s = refreshed_skip.get_player(1)
    assert alice_s.score == 1
    assert refreshed_skip.state == RoomState.MATCH_OVER


@pytest.mark.asyncio
async def test_no_turn_panel_sent_after_win_and_no_pass_accepted_after_win(temp_db, mock_bot):
    """
    No turn panel is sent after a win, and no pass after the win is accepted.
    """
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    chat_id = -100106
    p1 = Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0)
    p2 = Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1)
    p3 = Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2)
    p4 = Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3)

    p1.cards = [
        Card.create(0, 0),
        Card.create(0, 1),
        Card.create(0, 2),
        Card.create(0, 3),
        Card.create(1, 0),
    ]
    p2.cards = [Card.create(2, 0), Card.create(2, 1), Card.create(3, 0), Card.create(3, 1)]
    p3.cards = [Card.create(4, 0), Card.create(4, 1), Card.create(5, 0), Card.create(5, 1)]
    p4.cards = [Card.create(6, 0), Card.create(6, 1), Card.create(7, 0), Card.create(7, 1)]

    room = Room(
        chat_id=chat_id,
        host_id=1,
        state=RoomState.PLAYING,
        total_rounds=1,
        current_round=1,
        active_player_id=1,
        turn_nonce=1,
        players=[p1, p2, p3, p4],
    )
    await temp_db.save_room(room)

    mock_bot.send_message.reset_mock()

    # Alice passes Mango and wins
    success, err, p_card, passer, recv = await service.process_pass(
        chat_id=chat_id,
        passer_id=1,
        card_id="c_1_0",
        nonce=1,
    )
    assert success is True

    # Verify that NO turn panel was sent for Bob (the receiver)
    for call in mock_bot.send_message.call_args_list:
        text = call.kwargs.get("text", "")
        if "⏩ Next player:" in text:
            assert "Bob" not in text

    # Attempt another pass from Bob after the round has ended
    late_pass_success, late_err, _, _, _ = await service.process_pass(
        chat_id=chat_id,
        passer_id=2,
        card_id=p2.cards[0].id,
        nonce=room.turn_nonce + 1,
    )
    assert late_pass_success is False


def test_win_message_correct_ordinal_rounds_1_to_10_and_display_name():
    """
    The win message uses the correct ordinal for rounds 1 to 10 and the Telegram display name.
    """
    expected_ordinals = {
        1: "1st",
        2: "2nd",
        3: "3rd",
        4: "4th",
        5: "5th",
        6: "6th",
        7: "7th",
        8: "8th",
        9: "9th",
        10: "10th",
    }

    # Test with first_name only
    player1 = Player(user_id=10, room_id=100, first_name="Saisha", username="saisha_tg")
    for round_num, ordinal in expected_ordinals.items():
        win_msg = msgs.get_round_win_announcement(round_num, player1, rank=0)
        expected_start = f"🏆 {ordinal} round is won by Saisha!"
        assert win_msg.startswith(expected_start)
        assert "@saisha_tg" not in win_msg
        assert "@" not in win_msg

    # Test with first_name + last_name
    player2 = Player(user_id=20, room_id=100, first_name="Saisha", last_name="Verma", username="saisha_v")
    for round_num, ordinal in expected_ordinals.items():
        win_msg2 = msgs.get_round_win_announcement(round_num, player2, rank=1)
        expected_start2 = f"🏆 {ordinal} round is won by Saisha Verma!"
        assert win_msg2.startswith(expected_start2)
        assert "@saisha_v" not in win_msg2
        assert "@" not in win_msg2


@pytest.mark.asyncio
async def test_no_claim_button_or_claim_inline_result_anywhere(temp_db, mock_bot):
    """
    No claim button or claim inline result exists anywhere.
    """
    await temp_db.init_db()
    service = GameService(db=temp_db, bot=mock_bot)
    handlers = BotHandlers(game_service=service)
    chat_id = -100107

    p1 = Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0)
    # Give Alice 4 of a kind (4 Apples)
    p1.cards = [Card.create(0, i) for i in range(4)]
    p2 = Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1)
    p3 = Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2)
    p4 = Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3)

    room = Room(
        chat_id=chat_id,
        host_id=1,
        state=RoomState.PLAYING,
        total_rounds=5,
        current_round=1,
        active_player_id=1,
        players=[p1, p2, p3, p4],
    )
    await temp_db.save_room(room)

    # 1. Check table keyboard: MUST NOT contain any claim button
    table_kb = kb.get_table_keyboard(room)
    for row in table_kb.inline_keyboard:
        for button in row:
            text = button.text.lower()
            callback_data = (button.callback_data or "").lower()
            assert "claim" not in text
            assert "victory" not in text
            assert "claim" not in callback_data

    # 2. Check inline query results: Alice holds 4 of a kind, MUST NOT return claim result
    iq_update = MagicMock(spec=Update)
    query = AsyncMock(spec=InlineQuery)
    query.from_user = User(id=1, first_name="Alice", is_bot=False)
    iq_update.inline_query = query
    await handlers.handle_inline_query(iq_update, MagicMock())

    answered_results = query.answer.call_args[1]["results"]
    for r in answered_results:
        title = getattr(r, "title", "").lower()
        desc = getattr(r, "description", "").lower()
        r_id = getattr(r, "id", "").lower()
        assert "claim" not in title
        assert "victory" not in title
        assert "claim" not in desc
        assert not r_id.startswith("c_")
