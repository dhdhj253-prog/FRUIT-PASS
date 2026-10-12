import unittest
import tempfile
import os
from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.database import Database
from fruit_pass.game_service import GameService
from fruit_pass.game_engine import GameEngine


class TestHostTransfer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self.temp_dir.name, "test_host.db")
        self.db = Database(db_path=self.db_file)
        await self.db.init_db()
        self.service = GameService(db=self.db, bot=None)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_host_leaves_lobby_transfers_to_next_player(self):
        chat_id = -100777
        # Alice is host (user_id=1)
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
        ]
        room = Room(chat_id=chat_id, host_id=1, state=RoomState.LOBBY, players=players)
        await self.db.save_room(room)

        # Alice leaves
        updated_room, msg = await self.service.leave_room(chat_id, user_id=1)
        self.assertIsNotNone(updated_room)
        # Host should now be Bob (user_id=2)
        self.assertEqual(updated_room.host_id, 2)
        self.assertEqual(len(updated_room.players), 2)
        self.assertEqual(updated_room.players[0].user_id, 2)
        self.assertEqual(updated_room.players[0].seat_order, 0)

    async def test_host_removed_transfers_to_next_active_player(self):
        chat_id = -100778
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0, is_flagged=True),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
            Player(user_id=5, room_id=chat_id, first_name="Evan", seat_order=4),
        ]
        room = Room(chat_id=chat_id, host_id=1, state=RoomState.PLAYING, players=players)
        # When host transfer is called directly on engine or via removal
        new_host = GameEngine.transfer_host(room)
        self.assertEqual(new_host.user_id, 2)
        self.assertEqual(room.host_id, 2)

    async def test_non_host_cannot_start_or_close(self):
        chat_id = -100779
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
        ]
        room = Room(chat_id=chat_id, host_id=1, state=RoomState.LOBBY, players=players)
        await self.db.save_room(room)

        # Bob (user_id=2) attempts to start
        room, err, starter, newly_seated = await self.service.start_match(chat_id, requester_id=2)
        self.assertIn("Only the host", err)

        # Bob attempts to close/lock lobby
        success, close_err, _ = await self.service.set_lobby_lock(chat_id, requester_id=2, lock=True)
        self.assertFalse(success)
        self.assertIn("Only the host", close_err)
