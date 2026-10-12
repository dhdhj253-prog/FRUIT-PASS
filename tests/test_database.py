import unittest
import tempfile
import os
import time
from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.database import Database


class TestDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self.temp_dir.name, "test_db.db")
        self.db = Database(db_path=self.db_file)
        await self.db.init_db()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_room_persistence_and_restore_across_restart(self):
        chat_id = -100999
        players = [
            Player(user_id=10, room_id=chat_id, first_name="Alice", seat_order=0, score=3, is_flagged=True),
            Player(user_id=20, room_id=chat_id, first_name="Bob", seat_order=1, score=1),
        ]
        players[0].cards = [Card.create(0, 0), Card.create(0, 1)]
        players[1].cards = [Card.create(1, 0), Card.create(1, 1)]

        room = Room(
            chat_id=chat_id,
            host_id=10,
            state=RoomState.PLAYING,
            total_rounds=8,
            current_round=3,
            active_player_id=20,
            turn_nonce=99,
            turn_deadline=time.time() + 45.0,
            turn_notified_stages="60",
            starting_player_offset=1,
            round_pass_count=7,
            players=players,
        )
        await self.db.save_room(room)

        # Simulate bot restart by creating a new database connection instance to same file
        new_db = Database(db_path=self.db_file)
        loaded_room = await new_db.load_room(chat_id)

        self.assertIsNotNone(loaded_room)
        self.assertEqual(loaded_room.chat_id, chat_id)
        self.assertEqual(loaded_room.host_id, 10)
        self.assertEqual(loaded_room.state, RoomState.PLAYING)
        self.assertEqual(loaded_room.total_rounds, 8)
        self.assertEqual(loaded_room.current_round, 3)
        self.assertEqual(loaded_room.active_player_id, 20)
        self.assertEqual(loaded_room.turn_nonce, 99)
        self.assertEqual(loaded_room.turn_notified_stages, "60")
        self.assertEqual(len(loaded_room.players), 2)

        # Check players and their cards
        alice = loaded_room.get_player(10)
        self.assertEqual(alice.score, 3)
        self.assertTrue(alice.is_flagged)
        self.assertEqual(len(alice.cards), 2)
        self.assertEqual(alice.cards[0].name, "Apple")

        bob = loaded_room.get_player(20)
        self.assertEqual(bob.score, 1)
        self.assertFalse(bob.is_flagged)
        self.assertEqual(len(bob.cards), 2)
        self.assertEqual(bob.cards[0].name, "Mango")

    async def test_cleanup_abandoned_rooms(self):
        chat_id_active = -1001
        chat_id_abandoned = -1002

        now = time.time()
        room_active = Room(chat_id=chat_id_active, host_id=1, last_activity_at=now)
        room_abandoned = Room(chat_id=chat_id_abandoned, host_id=2, last_activity_at=now - 4000)

        await self.db.save_room(room_active)
        await self.db.save_room(room_abandoned)

        # Clean up rooms idle for > 3600 seconds
        cleaned = await self.db.cleanup_abandoned_rooms(max_idle_seconds=3600)
        self.assertIn(chat_id_abandoned, cleaned)
        self.assertNotIn(chat_id_active, cleaned)

        # Verify abandoned is deleted
        self.assertIsNone(await self.db.load_room(chat_id_abandoned))
        self.assertIsNotNone(await self.db.load_room(chat_id_active))
