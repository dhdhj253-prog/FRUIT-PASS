import unittest
import tempfile
import os
from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.database import Database
from fruit_pass.game_service import GameService
from fruit_pass.config import FRUIT_RANKS
import fruit_pass.bot_messages as msgs


class TestSecurityAndLeaks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self.temp_dir.name, "test_security.db")
        self.db = Database(db_path=self.db_file)
        await self.db.init_db()
        self.service = GameService(db=self.db, bot=None)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def test_turn_announcement_never_leaks_cards(self):
        """
        Anti-cheat requirement:
        'The group sees only Mia passed a card to Leo, with no fruit name or emoji of the card.
         Group messages, logs, and history never contain card identities, including auto-passes.'
        """
        passer = Player(user_id=1, room_id=100, first_name="Mia")
        receiver = Player(user_id=2, room_id=100, first_name="Leo")

        announcement = msgs.get_turn_announcement(passer, receiver)

        # Must mention players
        self.assertIn("Mia", announcement)
        self.assertIn("Leo", announcement)

        # Must NOT contain any fruit names or fruit emojis
        for _, name, emoji in FRUIT_RANKS:
            self.assertNotIn(name.lower(), announcement.lower())
            self.assertNotIn(emoji, announcement)

    def test_autopass_announcement_never_leaks_cards(self):
        passer = Player(user_id=1, room_id=100, first_name="Leo")
        announcement = msgs.get_autopass_announcement(passer)

        self.assertIn("Leo", announcement)
        for _, name, emoji in FRUIT_RANKS:
            self.assertNotIn(name.lower(), announcement.lower())
            self.assertNotIn(emoji, announcement)

    def test_round_win_reveals_only_winner_hand(self):
        winner = Player(user_id=2, room_id=100, first_name="Leo")
        win_msg = msgs.get_round_win_announcement(3, winner, rank=1)
        self.assertIn("Leo", win_msg)
        self.assertIn("Round 3", win_msg)
        self.assertIn("Mango", win_msg)
        self.assertIn("🥭", win_msg)

    async def test_stale_callback_rejected(self):
        chat_id = -100888
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
        ]
        players[0].cards = [Card.create(0, 0), Card.create(0, 1), Card.create(1, 0), Card.create(1, 1)]
        room = Room(
            chat_id=chat_id,
            host_id=1,
            state=RoomState.PLAYING,
            total_rounds=5,
            current_round=1,
            active_player_id=1,
            turn_nonce=42,  # Current nonce is 42
            players=players,
        )
        await self.db.save_room(room)

        # Client clicks old button with stale nonce = 41
        card_id = players[0].cards[0].id
        success, err, _, _, _ = await self.service.process_pass(
            chat_id, passer_id=1, card_id=card_id, nonce=41
        )
        self.assertFalse(success)
        self.assertIn("stale", err.lower())
