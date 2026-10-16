import unittest
import tempfile
import os
from unittest.mock import AsyncMock, MagicMock
from telegram import Update, User, InlineQuery, ChosenInlineResult

from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.database import Database
from fruit_pass.game_service import GameService
from fruit_pass.game_engine import GameEngine
from fruit_pass.bot_handlers import BotHandlers
from fruit_pass.config import FRUIT_RANKS


class TestInlineMode(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self.temp_dir.name, "test_inline.db")
        self.db = Database(db_path=self.db_file)
        await self.db.init_db()
        self.bot_mock = AsyncMock()
        self.service = GameService(db=self.db, bot=self.bot_mock)
        self.handlers = BotHandlers(game_service=self.service)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def _setup_active_room(self, chat_id=100) -> Room:
        players = [
            Player(user_id=1, room_id=chat_id, first_name="Alice", seat_order=0),
            Player(user_id=2, room_id=chat_id, first_name="Bob", seat_order=1),
            Player(user_id=3, room_id=chat_id, first_name="Charlie", seat_order=2),
            Player(user_id=4, room_id=chat_id, first_name="Diana", seat_order=3),
        ]
        room = Room(
            chat_id=chat_id,
            host_id=1,
            state=RoomState.LOBBY,
            total_rounds=5,
            current_round=0,
            table_message_id=999,
            players=players,
        )
        GameEngine.deal_round(room, 1)
        for i, uid in enumerate([1, 2, 3, 4]):
            p = room.get_player(uid)
            p.seat_order = i
        room.active_player_id = 1
        alice = room.get_player(1)
        # Alice holds 5 cards on turn
        alice.cards.append(Card.create(4, 0))
        alice.sort_hand()
        room.turn_nonce = 10
        await self.db.save_room(room)
        return room

    async def test_inline_query_returns_own_cards_is_personal_cache_time_zero(self):
        """
        inline_query returns only the asking user's own cards and is_personal=true, cache_time=0.
        """
        await self._setup_active_room()

        # Bob (user_id=2) asks for cards
        update = MagicMock(spec=Update)
        query = AsyncMock(spec=InlineQuery)
        query.from_user = User(id=2, first_name="Bob", is_bot=False)
        update.inline_query = query

        context = MagicMock()
        await self.handlers.handle_inline_query(update, context)

        # Check query.answer call arguments
        query.answer.assert_called_once()
        _, kwargs = query.answer.call_args
        self.assertTrue(kwargs.get("is_personal"))
        self.assertEqual(kwargs.get("cache_time"), 0)

        results = kwargs.get("results")
        self.assertEqual(len(results), 4)  # Bob has 4 cards

        # Results must reflect Bob's actual cards
        room = await self.db.load_room(100)
        bob = room.get_player(2)
        expected_titles = [f"{c.emoji} {c.name}" for c in sorted(bob.cards, key=lambda x: (x.rank, x.id))]
        actual_titles = [r.title for r in results]
        self.assertEqual(actual_titles, expected_titles)

    async def test_non_active_player_cannot_pass_and_stale_tokens_ignored(self):
        """
        Non-active player cannot pass via a result; stale or replayed tokens are ignored.
        """
        room = await self._setup_active_room()

        # Bob is non-active player. Bob requests inline query
        update = MagicMock(spec=Update)
        query = AsyncMock(spec=InlineQuery)
        query.from_user = User(id=2, first_name="Bob", is_bot=False)
        update.inline_query = query
        context = MagicMock()
        await self.handlers.handle_inline_query(update, context)

        results = query.answer.call_args[1]["results"]
        # All of Bob's cards are view-only with description "Not your turn."
        for r in results:
            self.assertEqual(r.description, "Not your turn.")

        bob_token = results[0].id

        # Simulating chosen_inline_result with Bob's token
        cir_update = MagicMock(spec=Update)
        cir = MagicMock(spec=ChosenInlineResult)
        cir.result_id = bob_token
        cir.from_user = User(id=2, first_name="Bob", is_bot=False)
        cir_update.chosen_inline_result = cir

        await self.handlers.handle_chosen_inline_result(cir_update, context)

        # Room turn must remain on Alice (user_id=1), turn_nonce unchanged
        refreshed = await self.db.load_room(room.chat_id)
        self.assertEqual(refreshed.active_player_id, 1)
        self.assertEqual(refreshed.turn_nonce, 10)

        # Test stale token: Alice's pass token with old nonce
        stale_token = "p_9_abcdef123456"
        self.service.register_inline_token(
            stale_token,
            {"type": "pass", "chat_id": 100, "user_id": 1, "nonce": 9, "card_id": "c_0_0"}
        )
        cir.result_id = stale_token
        cir.from_user = User(id=1, first_name="Alice", is_bot=False)
        await self.handlers.handle_chosen_inline_result(cir_update, context)

        # Still unchanged
        refreshed = await self.db.load_room(room.chat_id)
        self.assertEqual(refreshed.turn_nonce, 10)

    async def test_neutral_message_content_never_contains_fruit_name_or_emoji(self):
        """
        Neutral message content never contains a fruit name or emoji.
        """
        await self._setup_active_room()

        # Alice (active) query
        update = MagicMock(spec=Update)
        query = AsyncMock(spec=InlineQuery)
        query.from_user = User(id=1, first_name="Alice", is_bot=False)
        update.inline_query = query
        await self.handlers.handle_inline_query(update, MagicMock())

        pass_results = query.answer.call_args[1]["results"]

        # Bob (non-active) query
        query_bob = AsyncMock(spec=InlineQuery)
        query_bob.from_user = User(id=2, first_name="Bob", is_bot=False)
        update.inline_query = query_bob
        await self.handlers.handle_inline_query(update, MagicMock())

        view_results = query_bob.answer.call_args[1]["results"]

        all_results = pass_results + view_results

        for r in all_results:
            msg_text = r.input_message_content.message_text
            for _, name, emoji in FRUIT_RANKS:
                self.assertNotIn(name.lower(), msg_text.lower(), f"Fruit name {name} found in '{msg_text}'")
                self.assertNotIn(emoji, msg_text, f"Fruit emoji {emoji} found in '{msg_text}'")

    async def test_user_in_two_rooms_rejected(self):
        """
        A user in two rooms is rejected.
        """
        # User 1 is host of Room 100
        await self.service.create_room(chat_id=100, host_id=1, host_first_name="Alice")

        # User 1 tries to join Room 200 (created by host 2)
        await self.service.create_room(chat_id=200, host_id=2, host_first_name="Bob")

        room, msg = await self.service.join_room(chat_id=200, user_id=1, first_name="Alice")
        self.assertIsNone(room)
        self.assertIn("already in an active match", msg.lower())

    async def test_chosen_inline_result_for_autopassed_turn_ignored(self):
        """
        chosen_inline_result for an auto-passed turn is ignored.
        """
        room = await self._setup_active_room()

        # Alice query generates pass token for turn_nonce=10
        update = MagicMock(spec=Update)
        query = AsyncMock(spec=InlineQuery)
        query.from_user = User(id=1, first_name="Alice", is_bot=False)
        update.inline_query = query
        await self.handlers.handle_inline_query(update, MagicMock())

        pass_results = query.answer.call_args[1]["results"]
        alice_token = pass_results[0].id

        # Timer fires auto-pass for Alice first
        success, passer, receiver = await self.service.process_autopass(room.chat_id)
        self.assertTrue(success)
        self.assertEqual(passer.user_id, 1)
        self.assertEqual(receiver.user_id, 2)

        refreshed_after_auto = await self.db.load_room(room.chat_id)
        self.assertEqual(refreshed_after_auto.turn_nonce, 11)
        self.assertEqual(refreshed_after_auto.active_player_id, 2)

        # Now Alice's late chosen_inline_result arrives with token from nonce 10
        cir_update = MagicMock(spec=Update)
        cir = MagicMock(spec=ChosenInlineResult)
        cir.result_id = alice_token
        cir.from_user = User(id=1, first_name="Alice", is_bot=False)
        cir_update.chosen_inline_result = cir

        await self.handlers.handle_chosen_inline_result(cir_update, MagicMock())

        # State remains at nonce 11, active player Bob (user_id=2)
        refreshed_final = await self.db.load_room(room.chat_id)
        self.assertEqual(refreshed_final.turn_nonce, 11)
        self.assertEqual(refreshed_final.active_player_id, 2)

    async def test_claim_result_never_appears_even_when_four_of_a_kind(self):
        """
        Claim result never appears, even when the hand is four of a kind.
        """
        room = await self._setup_active_room()

        # Charlie (user_id=3) currently has cards: 2x rank 1, 2x rank 2 (not 4-of-a-kind)
        update = MagicMock(spec=Update)
        query = AsyncMock(spec=InlineQuery)
        query.from_user = User(id=3, first_name="Charlie", is_bot=False)
        update.inline_query = query
        await self.handlers.handle_inline_query(update, MagicMock())

        results = query.answer.call_args[1]["results"]
        # No claim button present
        self.assertFalse(any("claim" in r.title.lower() for r in results))

        # Give Charlie 4 Melons (rank 7) (4-of-a-kind)
        charlie = room.get_player(3)
        charlie.cards = [Card.create(7, i) for i in range(4)]
        await self.db.save_room(room)

        # Charlie queries again
        query2 = AsyncMock(spec=InlineQuery)
        query2.from_user = User(id=3, first_name="Charlie", is_bot=False)
        update.inline_query = query2
        await self.handlers.handle_inline_query(update, MagicMock())

        results2 = query2.answer.call_args[1]["results"]
        # No claim result exists anywhere even when holding four of a kind
        self.assertFalse(any("claim" in r.title.lower() for r in results2))


if __name__ == "__main__":
    unittest.main()
