import unittest
import tempfile
import os
from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.database import Database
from fruit_pass.game_service import GameService
from fruit_pass.game_engine import GameEngine


class TestFlaggingAndRemoval(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self.temp_dir.name, "test_flagging.db")
        self.db = Database(db_path=self.db_file)
        await self.db.init_db()
        self.service = GameService(db=self.db, bot=None)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_flagging_after_two_consecutive_autopasses(self):
        chat_id = -100444
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
        ]
        room = Room(chat_id=chat_id, host_id=1, players=players)
        GameEngine.deal_round(room, round_number=1)
        for i, uid in enumerate([1, 2, 3, 4]):
            room.get_player(uid).seat_order = i
        room.active_player_id = 1
        await self.db.save_room(room)

        # 1st auto-pass for Alice
        await self.service.process_autopass(chat_id)
        room = await self.db.load_room(chat_id)
        alice = room.get_player(1)
        self.assertEqual(alice.consecutive_autopasses, 1)
        self.assertFalse(alice.is_flagged)

        # Pass from Bob to Charlie to Diana back to Alice so Alice is active again
        GameEngine.pass_card(room, 2, room.get_player(2).cards[0].id)
        GameEngine.pass_card(room, 3, room.get_player(3).cards[0].id)
        GameEngine.pass_card(room, 4, room.get_player(4).cards[0].id)
        self.assertEqual(room.active_player_id, 1)
        await self.db.save_room(room)

        # 2nd consecutive auto-pass for Alice
        await self.service.process_autopass(chat_id)
        room = await self.db.load_room(chat_id)
        alice = room.get_player(1)
        self.assertEqual(alice.consecutive_autopasses, 2)
        self.assertTrue(alice.is_flagged)  # Alice is now flagged ⚠️

        # Test unflagging: Alice performs a manual pass later
        # Cycle back to Alice
        GameEngine.pass_card(room, 2, room.get_player(2).cards[0].id)
        GameEngine.pass_card(room, 3, room.get_player(3).cards[0].id)
        GameEngine.pass_card(room, 4, room.get_player(4).cards[0].id)
        await self.db.save_room(room)

        # Alice manually passes
        alice_card_id = room.get_player(1).cards[0].id
        success, _, _, _, _ = await self.service.process_pass(
            chat_id, passer_id=1, card_id=alice_card_id, nonce=room.turn_nonce
        )
        self.assertTrue(success)
        room = await self.db.load_room(chat_id)
        alice = room.get_player(1)
        self.assertFalse(alice.is_flagged)
        self.assertEqual(alice.consecutive_autopasses, 0)

    async def test_mid_round_removal_with_five_players_redeal(self):
        """
        5 players: Alice, Bob, Charlie, Diana, Evan.
        Evan is flagged. Host removes Evan.
        4 players remain (>= MIN_PLAYERS), so round is voided and fresh round dealt.
        """
        chat_id = -100555
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
            Player(user_id=5, room_id=chat_id, first_name="Evan", seat_order=4, is_flagged=True, score=2),
        ]
        room = Room(chat_id=chat_id, host_id=1, players=players)
        GameEngine.deal_round(room, round_number=2)
        await self.db.save_room(room)

        # Alice (host) removes Evan
        success, msg, round_voided, can_continue = await self.service.remove_player(
            chat_id=chat_id, host_id=1, target_user_id=5
        )
        self.assertTrue(success)
        self.assertTrue(round_voided)
        self.assertTrue(can_continue)

        room = await self.db.load_room(chat_id)
        self.assertEqual(len(room.active_players), 4)
        evan = room.get_player(5)
        self.assertTrue(evan.is_removed)
        self.assertEqual(evan.score, 2)  # Keeps previous score
        # Fresh round dealt with 4 players: total cards in active hands = 16
        total_active_cards = sum(len(p.cards) for p in room.active_players)
        self.assertEqual(total_active_cards, 16)
        self.assertEqual(room.state, RoomState.PLAYING)

    async def test_mid_round_removal_fewer_than_four_pauses_match(self):
        """
        4 players. Host removes flagged player -> 3 remain (< MIN_PLAYERS).
        Match must enter PAUSED state.
        """
        chat_id = -100666
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3, is_flagged=True),
        ]
        room = Room(chat_id=chat_id, host_id=1, players=players)
        GameEngine.deal_round(room, round_number=1)
        await self.db.save_room(room)

        success, msg, round_voided, can_continue = await self.service.remove_player(
            chat_id=chat_id, host_id=1, target_user_id=4
        )
        self.assertTrue(success)
        self.assertTrue(round_voided)
        self.assertFalse(can_continue)

        room = await self.db.load_room(chat_id)
        self.assertEqual(room.state, RoomState.PAUSED)
        self.assertIsNone(room.active_player_id)
        self.assertIsNone(room.turn_deadline)
