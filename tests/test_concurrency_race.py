import asyncio
import unittest
import tempfile
import os
from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.database import Database
from fruit_pass.game_service import GameService


class TestConcurrency(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self.temp_dir.name, "test_concurrency.db")
        self.db = Database(db_path=self.db_file)
        await self.db.init_db()
        self.service = GameService(db=self.db, bot=None)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_simultaneous_claims_race_safety(self):
        chat_id = -100999

        # Setup room with 4 players
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
        ]
        # Give Alice 4 Apples (valid 4-of-a-kind)
        players[0].cards = [Card.create(0, i) for i in range(4)]
        # Give Bob 4 Mangos (also valid 4-of-a-kind)
        players[1].cards = [Card.create(1, i) for i in range(4)]
        # Give Charlie & Diana mixed
        players[2].cards = [Card.create(2, 0), Card.create(2, 1), Card.create(3, 0), Card.create(3, 1)]
        players[3].cards = [Card.create(2, 2), Card.create(2, 3), Card.create(3, 2), Card.create(3, 3)]

        room = Room(
            chat_id=chat_id,
            host_id=1,
            state=RoomState.PLAYING,
            total_rounds=5,
            current_round=1,
            active_player_id=1,
            turn_nonce=10,
            players=players,
        )
        await self.db.save_room(room)

        # Both Alice and Bob attempt to claim victory simultaneously!
        claim_tasks = [
            self.service.process_claim_victory(chat_id, claimer_id=1, nonce=10),
            self.service.process_claim_victory(chat_id, claimer_id=2, nonce=10),
        ]

        results = await asyncio.gather(*claim_tasks)

        # Exactly ONE claim must succeed and one must be rejected!
        success_count = sum(1 for res in results if res[0] is True)
        self.assertEqual(success_count, 1, f"Expected exactly 1 claim to succeed, got {results}")

        # Load room and check scores
        updated_room = await self.db.load_room(chat_id)
        alice = updated_room.get_player(1)
        bob = updated_room.get_player(2)

        total_score = alice.score + bob.score
        self.assertEqual(total_score, 1, "Only one point must have been awarded across the round!")

    async def test_concurrent_passes_are_serialized(self):
        chat_id = -100888

        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
        ]
        # Alice has cards
        players[0].cards = [Card.create(0, 0), Card.create(0, 1), Card.create(1, 0), Card.create(1, 1)]
        players[1].cards = [Card.create(2, 0), Card.create(2, 1), Card.create(3, 0), Card.create(3, 1)]

        room = Room(
            chat_id=chat_id,
            host_id=1,
            state=RoomState.PLAYING,
            total_rounds=5,
            current_round=1,
            active_player_id=1,
            turn_nonce=5,
            players=players,
        )
        await self.db.save_room(room)

        # Two requests from Alice attempting to pass the same card with the same nonce
        card_id = players[0].cards[0].id
        pass_tasks = [
            self.service.process_pass(chat_id, passer_id=1, card_id=card_id, nonce=5),
            self.service.process_pass(chat_id, passer_id=1, card_id=card_id, nonce=5),
        ]
        results = await asyncio.gather(*pass_tasks)

        # Only one pass can succeed; the other must fail
        successes = [r for r in results if r[0] is True]
        self.assertEqual(len(successes), 1)
