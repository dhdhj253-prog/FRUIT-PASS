import unittest
import tempfile
import os
import time
from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.database import Database
from fruit_pass.game_service import GameService
from fruit_pass.timer_service import TimerService
from fruit_pass.game_engine import GameEngine


class TestTurnAndAutoPass(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self.temp_dir.name, "test_autopass.db")
        self.db = Database(db_path=self.db_file)
        await self.db.init_db()
        self.service = GameService(db=self.db, bot=None)
        self.timer_service = TimerService(db=self.db, game_service=self.service, bot=None)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_autopass_triggers_and_passes_random_card(self):
        chat_id = -100111
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
        ]
        room = Room(chat_id=chat_id, host_id=1, players=players)
        GameEngine.deal_round(room, round_number=1)
        starter_id = room.active_player_id
        receiver = room.next_active_player_after(starter_id)
        # Simulate timer expiry
        room.turn_deadline = time.time() - 5.0  # 5s in past
        await self.db.save_room(room)

        # Trigger timer tick
        await self.timer_service.tick()

        # Check updated room
        updated_room = await self.db.load_room(chat_id)
        starter = updated_room.get_player(starter_id)
        recv = updated_room.get_player(receiver.user_id)

        # Starter auto-passed 1 card to receiver
        self.assertEqual(len(starter.cards), 3)
        self.assertEqual(len(recv.cards), 5)
        self.assertEqual(updated_room.active_player_id, receiver.user_id)  # Receiver is now active
        self.assertEqual(starter.consecutive_autopasses, 1)

    async def test_idempotency_late_manual_pass_after_autopass(self):
        chat_id = -100222
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
        ]
        room = Room(chat_id=chat_id, host_id=1, players=players)
        GameEngine.deal_round(room, round_number=1)
        original_nonce = room.turn_nonce
        starter = room.get_active_player()
        card_id = starter.cards[0].id
        await self.db.save_room(room)

        # Auto-pass executes
        success, passer, receiver = await self.service.process_autopass(chat_id)
        self.assertTrue(success)

        # Now starter's client sends late manual pass for the old turn
        late_success, err, _, _, _ = await self.service.process_pass(
            chat_id, passer_id=starter.user_id, card_id=card_id, nonce=original_nonce
        )
        self.assertFalse(late_success)
        self.assertIn("stale", err.lower())

    async def test_autopass_counts_as_turn_and_allows_claim(self):
        chat_id = -100333
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
        ]
        # Give Alice 5 cards: 4 Apples and 1 Mango
        players[0].cards = [Card.create(0, 0), Card.create(0, 1), Card.create(0, 2), Card.create(0, 3), Card.create(1, 0)]
        players[1].cards = [Card.create(1, 1), Card.create(1, 2), Card.create(1, 3), Card.create(2, 0)]

        room = Room(
            chat_id=chat_id,
            host_id=1,
            state=RoomState.PLAYING,
            total_rounds=5,
            current_round=1,
            active_player_id=1,
            turn_nonce=1,
            players=players,
        )
        await self.db.save_room(room)

        # Force auto-pass on the 1 Mango (card index 4) by making it pass that card
        # Or let GameEngine pass: if Alice passes the Mango, she has 4 Apples left
        mango_card = players[0].cards[4]
        # Test GameEngine pass_card
        GameEngine.pass_card(room, 1, mango_card.id)
        await self.db.save_room(room)

        # Now Alice has exactly four Apples!
        self.assertEqual(len(room.get_player(1).cards), 4)
        is_valid, err, rank, is_match_over, winners = await self.service.process_claim_victory(chat_id, claimer_id=1)
        self.assertTrue(is_valid)
        self.assertEqual(rank, 0)
