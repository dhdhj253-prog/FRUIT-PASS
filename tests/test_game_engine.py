import pytest
from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.game_engine import GameEngine
from fruit_pass.config import FRUIT_RANKS, MIN_PLAYERS, MAX_PLAYERS


def test_deck_size_4_players():
    deck = GameEngine.create_deck(4)
    assert len(deck) == 16
    ranks = [c.rank for c in deck]
    for r in range(4):
        assert ranks.count(r) == 4
    # All card IDs must be unique
    ids = [c.id for c in deck]
    assert len(set(ids)) == 16


def test_deck_size_6_players():
    deck = GameEngine.create_deck(6)
    assert len(deck) == 24
    ranks = [c.rank for c in deck]
    for r in range(6):
        assert ranks.count(r) == 4
    ids = [c.id for c in deck]
    assert len(set(ids)) == 24


def test_deck_size_8_players():
    deck = GameEngine.create_deck(8)
    assert len(deck) == 32
    ranks = [c.rank for c in deck]
    for r in range(8):
        assert ranks.count(r) == 4
    ids = [c.id for c in deck]
    assert len(set(ids)) == 32


def test_deck_invalid_player_count():
    with pytest.raises(ValueError):
        GameEngine.create_deck(3)
    with pytest.raises(ValueError):
        GameEngine.create_deck(9)


def test_deal_round_hands_and_seating_order():
    players = [
        Player(user_id=1, room_id=100, first_name="Alice", seat_order=0),
        Player(user_id=2, room_id=100, first_name="Bob", seat_order=1),
        Player(user_id=3, room_id=100, first_name="Charlie", seat_order=2),
        Player(user_id=4, room_id=100, first_name="Diana", seat_order=3),
    ]
    room = Room(
        chat_id=100,
        host_id=1,
        total_rounds=5,
        players=players,
    )

    # Deal Round 1
    starter, newly_seated = GameEngine.deal_round(room, round_number=1)
    assert room.state == RoomState.PLAYING
    assert room.current_round == 1
    assert room.active_player_id == starter.user_id
    assert starter.user_id in [1, 2, 3, 4]

    # Each player should have 4 cards
    for p in room.players:
        assert len(p.cards) == 4

    # Starter should be first in seat_order
    assert room.players[0].user_id == starter.user_id
    assert room.players[0].seat_order == 0


def test_turn_order_and_passing():
    players = [
        Player(user_id=1, room_id=100, first_name="Alice", seat_order=0),
        Player(user_id=2, room_id=100, first_name="Bob", seat_order=1),
        Player(user_id=3, room_id=100, first_name="Charlie", seat_order=2),
        Player(user_id=4, room_id=100, first_name="Diana", seat_order=3),
    ]
    room = Room(chat_id=100, host_id=1, players=players)
    starter, _ = GameEngine.deal_round(room, round_number=1)

    passer = starter
    receiver = room.next_active_player_after(passer.user_id)
    assert receiver is not None
    card_to_pass = passer.cards[0]

    # Starter passes to receiver
    passed_card, p_out, r_out = GameEngine.pass_card(room, passer.user_id, card_to_pass.id)
    assert passed_card.id == card_to_pass.id
    assert p_out.user_id == passer.user_id
    assert r_out.user_id == receiver.user_id
    assert len(passer.cards) == 3
    assert len(receiver.cards) == 5
    assert room.active_player_id == receiver.user_id

    # Receiver passes to next
    next_receiver = room.next_active_player_after(receiver.user_id)
    assert next_receiver is not None
    rec_card = receiver.cards[0]
    passed_card2, p_out2, r_out2 = GameEngine.pass_card(room, receiver.user_id, rec_card.id)
    assert p_out2.user_id == receiver.user_id
    assert r_out2.user_id == next_receiver.user_id
    assert len(receiver.cards) == 4
    assert len(next_receiver.cards) == 5
    assert room.active_player_id == next_receiver.user_id


def test_victory_validation():
    players = [
        Player(user_id=1, room_id=100, first_name="Alice", seat_order=0),
        Player(user_id=2, room_id=100, first_name="Bob", seat_order=1),
        Player(user_id=3, room_id=100, first_name="Charlie", seat_order=2),
        Player(user_id=4, room_id=100, first_name="Diana", seat_order=3),
    ]
    room = Room(chat_id=100, host_id=1, state=RoomState.PLAYING, players=players)

    # Bob has 4 Apples (rank 0)
    bob = room.get_player(2)
    bob.cards = [Card.create(0, i) for i in range(4)]
    bob.sort_hand()

    # Valid claim
    is_valid, win_rank, err = GameEngine.validate_claim_victory(room, 2)
    assert is_valid is True
    assert win_rank == 0
    assert err is None
    assert bob.score == 1  # Awarded 1 point

    # Alice has 3 Apples, 1 Mango -> Invalid
    alice = room.get_player(1)
    alice.cards = [Card.create(0, 0), Card.create(0, 1), Card.create(0, 2), Card.create(1, 0)]
    is_valid_a, win_rank_a, err_a = GameEngine.validate_claim_victory(room, 1)
    assert is_valid_a is False
    assert win_rank_a is None
    assert "not four of a kind" in err_a

    # Player with 5 cards cannot claim (must pass first)
    bob.cards.append(Card.create(1, 1))
    is_valid_b, _, _ = GameEngine.validate_claim_victory(room, 2)
    assert is_valid_b is False

    # Player with 3 cards cannot claim
    alice.cards.pop()
    alice.cards.pop()
    assert len(alice.cards) == 2
    is_valid_c, _, _ = GameEngine.validate_claim_victory(room, 1)
    assert is_valid_c is False
