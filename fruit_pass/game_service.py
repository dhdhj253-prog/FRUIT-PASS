from __future__ import annotations
import asyncio
import time
import logging
from typing import Dict, Optional, Tuple, List
from telegram import Bot
from telegram.error import TelegramError

from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.config import (
    MIN_PLAYERS,
    MAX_PLAYERS,
    TURN_TIMEOUT_SECONDS,
    RANK_NAME_BY_INDEX,
    RANK_EMOJI_BY_INDEX,
    BOT_USERNAME,
    CLEANUP_ENABLED,
)
from fruit_pass.game_engine import GameEngine
from fruit_pass.database import Database
import fruit_pass.bot_messages as msgs
import fruit_pass.bot_keyboards as kb

logger = logging.getLogger(__name__)


class GameService:
    def __init__(self, db: Database, bot: Optional[Bot] = None):
        self.db = db
        self.bot = bot
        self._locks: Dict[int, asyncio.Lock] = {}
        # Server-side token registry for inline queries (opaque tokens -> actions)
        self._inline_tokens: Dict[str, dict] = {}
        # In-memory transient admin status cache: (chat_id, user_id) -> (is_admin, timestamp)
        self._admin_cache: Dict[Tuple[int, int], Tuple[bool, float]] = {}

    def invalidate_admin_cache(self, chat_id: Optional[int] = None, user_id: Optional[int] = None) -> None:
        """
        Invalidates cached admin status for (chat_id, user_id), an entire chat, or all chats.
        """
        if chat_id is not None and user_id is not None:
            self._admin_cache.pop((chat_id, user_id), None)
        elif chat_id is not None:
            to_del = [k for k in self._admin_cache if k[0] == chat_id]
            for k in to_del:
                self._admin_cache.pop(k, None)
        else:
            self._admin_cache.clear()

    async def is_privileged_user(
        self, chat_id: int, user_id: Optional[int] = None, sender_chat_id: Optional[int] = None
    ) -> bool:
        """
        A privileged user is:
        - Anonymous admin: sender_chat_id == chat_id
        - Room host: room.host_id == user_id
        - Group admin or creator: status in ['administrator', 'creator'] via getChatMember
        Cached for at most 60 seconds.
        """
        # Anonymous admin check
        if isinstance(sender_chat_id, int) and sender_chat_id == chat_id:
            return True

        if user_id is None:
            return False

        # Room host check
        room = await self.db.load_room(chat_id)
        if room and room.host_id == user_id:
            return True

        # Check transient cache (TTL <= 60s)
        now = time.time()
        cache_key = (chat_id, user_id)
        if cache_key in self._admin_cache:
            is_admin, cached_at = self._admin_cache[cache_key]
            if now - cached_at < 60.0:
                return is_admin

        # Check live via getChatMember
        if self.bot and hasattr(self.bot, "get_chat_member"):
            try:
                res = self.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                member = await res if (asyncio.iscoroutine(res) or hasattr(res, "__await__")) else res
                status = getattr(member, "status", None)
                status_str = str(getattr(status, "value", status)).lower()
                is_admin = status_str in ["administrator", "creator"] or status in ["administrator", "creator"]
                self._admin_cache[cache_key] = (is_admin, now)
                return is_admin
            except Exception as e:
                logger.warning("Error fetching chat member %s in %s: %s", user_id, chat_id, e)
                return False

        return False

    def get_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    def register_inline_token(self, token: str, data: dict) -> None:
        """
        Maps a short opaque token (<= 64 bytes) to action payload server-side.
        """
        self._inline_tokens[token] = data

    def get_inline_token(self, token: str) -> Optional[dict]:
        """
        Looks up a registered inline token.
        """
        return self._inline_tokens.get(token)

    def invalidate_room_tokens(self, chat_id: int) -> None:
        """
        Evicts tokens for a given room.
        """
        keys_to_remove = [k for k, v in self._inline_tokens.items() if v.get("chat_id") == chat_id]
        for k in keys_to_remove:
            self._inline_tokens.pop(k, None)

    async def send_turn_panel(self, room: Room, player: Player) -> Optional[int]:
        """
        Sends the bot's turn panel in the group tagging the next player.
        Text, exactly one line: "⏩ Next player: [mention of Name B]"
        Attach one inline keyboard button: "Make your choice!" (switch_inline_query_current_chat="")
        If CLEANUP_ENABLED is True, deletes previous turn panel message.
        """
        if not self.bot:
            return None

        if CLEANUP_ENABLED and room.last_ping_message_id:
            try:
                await self.bot.delete_message(
                    chat_id=room.chat_id,
                    message_id=room.last_ping_message_id,
                )
            except Exception:
                pass
            room.last_ping_message_id = None

        text = msgs.get_turn_panel_text(player)
        reply_markup = kb.get_turn_panel_keyboard()
        try:
            sent_msg = await self.bot.send_message(
                chat_id=room.chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            msg_id = getattr(sent_msg, "message_id", None)
            if msg_id is not None:
                room.last_ping_message_id = int(msg_id)
            await self.db.save_room(room)
            return msg_id
        except Exception as e:
            logger.error("Error sending turn panel in room %s: %s", room.chat_id, e)
            return None

    # Alias for backwards compatibility
    send_turn_ping = send_turn_panel

    async def update_table_message(self, room: Room, last_event: Optional[str] = None) -> None:
        """
        Updates the in-place public table message for the room in group chat.
        """
        if not self.bot or not room.table_message_id:
            return

        text = msgs.get_table_text(room, last_event=last_event)
        reply_markup = kb.get_table_keyboard(room)
        try:
            await self.bot.edit_message_text(
                chat_id=room.chat_id,
                message_id=room.table_message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        except TelegramError as e:
            if "Message is not modified" not in str(e):
                logger.warning("Error editing table message in room %s: %s", room.chat_id, e)

    async def check_user_registered_or_probe(
        self, user_id: int, first_name: str, username: Optional[str] = None
    ) -> bool:
        """
        Join gate check order:
        1. Registry lookup: if active, seat them.
        2. If not in the registry, probe with sendChatAction(chat_id=user_id, action="typing").
           If it succeeds, the user has already started the bot: register as active and seat them.
        3. Only if the probe fails with 403 (never started or blocked) return False.
        """
        user_record = await self.db.get_user(user_id)
        if user_record and user_record.get("status") == "active":
            return True

        if self.bot:
            try:
                await self.bot.send_chat_action(chat_id=user_id, action="typing")
                await self.db.register_or_activate_user(user_id, first_name, username)
                return True
            except TelegramError as e:
                err_str = str(e).lower()
                if "forbidden" in err_str or "blocked" in err_str or "initiate" in err_str or "403" in err_str:
                    await self.db.set_user_status(user_id, "blocked")
                    return False
                logger.warning("Error probing chat action for user %s: %s", user_id, e)
                return False
            except Exception as e:
                err_str = str(e).lower()
                if "forbidden" in err_str or "blocked" in err_str or "initiate" in err_str:
                    await self.db.set_user_status(user_id, "blocked")
                    return False
                logger.warning("Unexpected error probing chat action for user %s: %s", user_id, e)
                return False
        return False

    async def update_lobby_message(self, chat_id: int) -> None:
        """
        After every join, leave, /open, /close, and round-count change, edit the lobby message in place.
        Handle 'message is not modified' quietly.
        If the message was deleted or can't be edited, send a new lobby message and store its message_id.
        """
        if not self.bot:
            return

        room = await self.db.load_room(chat_id)
        if not room or room.state != RoomState.LOBBY:
            return

        text = msgs.get_lobby_text(room)
        reply_markup = kb.get_lobby_keyboard(room, BOT_USERNAME)

        if not room.lobby_message_id:
            return

        try:
            await self.bot.edit_message_text(
                chat_id=room.chat_id,
                message_id=room.lobby_message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return
        except TelegramError as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str:
                return
            logger.info("Could not edit lobby message in room %s (%s). Sending new message.", room.chat_id, e)

        # Message was deleted or can't be edited -> send new lobby message and store its message_id
        try:
            sent_msg = await self.bot.send_message(
                chat_id=room.chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            room.lobby_message_id = sent_msg.message_id
            await self.db.save_room(room)
        except Exception as e:
            logger.error("Error sending new lobby message in room %s: %s", room.chat_id, e)

    async def send_round_transition_messages(
        self,
        room: Room,
        starter: Player,
        newly_seated: List[Player],
        round_win_msg: Optional[str] = None,
    ) -> None:
        """
        Sends transition messages in sequence about 1s apart:
        a) Win announcement (if provided)
        b) "🔀 Shuffling players for Round N..."
        c) "➕ [Name] joins from this round." (if any)
        d) "🎲 New pass order: [A] → [B] → [C] → [D] → [A]"
        e) "✅ Round N has now started! 🎴 @starter, you go first. ..."
        """
        if not self.bot:
            return

        # a) Existing win message
        if round_win_msg:
            try:
                await self.bot.send_message(chat_id=room.chat_id, text=round_win_msg, parse_mode="Markdown")
            except Exception as e:
                logger.error("Error sending win msg: %s", e)
            await asyncio.sleep(1.0)

        # b) Shuffling players
        try:
            await self.bot.send_message(
                chat_id=room.chat_id,
                text=msgs.get_shuffling_text(room.current_round),
            )
        except Exception as e:
            logger.error("Error sending shuffle msg: %s", e)
        await asyncio.sleep(1.0)

        # c) Newly seated mid-match players
        for p in newly_seated:
            try:
                await self.bot.send_message(
                    chat_id=room.chat_id,
                    text=msgs.get_player_joined_round_text(p),
                )
            except Exception as e:
                logger.error("Error sending seated join msg: %s", e)
            await asyncio.sleep(1.0)

        # d) New pass order
        try:
            await self.bot.send_message(
                chat_id=room.chat_id,
                text=msgs.get_new_pass_order_text(room.active_players),
            )
        except Exception as e:
            logger.error("Error sending pass order msg: %s", e)
        await asyncio.sleep(1.0)

        # e) End the sequence with "✅ Round N+1 has now started!" (or Round 1) (no tag)
        try:
            await self.bot.send_message(
                chat_id=room.chat_id,
                text=msgs.get_round_started_text(room.current_round),
            )
        except Exception as e:
            logger.error("Error sending round started msg: %s", e)

        # Then send the same turn panel for the starting player
        await self.send_turn_panel(room, starter)

    async def create_room(
        self,
        chat_id: int,
        host_id: int,
        host_first_name: str,
        host_username: Optional[str] = None,
        host_last_name: Optional[str] = None,
    ) -> Tuple[Room, bool]:
        """
        Creates a new room in a group. Returns (room, created_new).
        Rejects if host is already in another active room.
        If a previous game in this group ended or was closed, wipes it clean.
        """
        async with self.get_lock(chat_id):
            existing = await self.db.load_room(chat_id)
            if existing and existing.state not in [RoomState.CLOSED, RoomState.MATCH_OVER]:
                return existing, False

            # Previous game ended: wipe old room cleanly
            if existing:
                await self.db.delete_room(chat_id)

            active_room = await self.db.get_active_room_for_user(host_id)
            if active_room and active_room.chat_id != chat_id:
                return active_room, False

            room = Room(
                chat_id=chat_id,
                host_id=host_id,
                state=RoomState.LOBBY,
                total_rounds=5,
                current_round=0,
                active_player_id=None,
                turn_nonce=0,
                starting_player_offset=0,
                round_pass_count=0,
                is_lobby_locked=False,
                last_activity_at=time.time(),
                players=[],
            )
            # Host joins automatically
            host_player = Player(
                user_id=host_id,
                room_id=chat_id,
                username=host_username,
                first_name=host_first_name,
                last_name=host_last_name,
                seat_order=0,
                is_waiting=False,
            )
            room.players.append(host_player)
            await self.db.delete_room(chat_id)
            await self.db.save_room(room)
            await self.db.add_player_to_room_atomic(chat_id, host_id, host_player.display_name, host_username)
            return room, True

    async def join_room(
        self,
        chat_id: int,
        user_id: int,
        first_name: str,
        username: Optional[str] = None,
        check_registration: bool = False,
        last_name: Optional[str] = None,
    ) -> Tuple[Optional[Room], str]:
        """
        Player joins room. Returns (room, message).
        Enforces one active room per user and lobby locking.
        Queues mid-match joiners as waiting.
        """
        async with self.get_lock(chat_id):
            if check_registration:
                is_active = await self.check_user_registered_or_probe(user_id, first_name, username)
                if not is_active:
                    return None, "Start the bot first to join the game."

            room = await self.db.load_room(chat_id)
            if not room or room.state in [RoomState.CLOSED, RoomState.MATCH_OVER]:
                return None, "No active game room found in this group. Use /newgame to create one."

            # Show "already joined" only if the user is truly in the current room's player list.
            existing = room.get_player(user_id)
            if existing and not existing.is_removed:
                return room, "You're already in this game."

            # If active lock comes from an old room, clear it
            active_room = await self.db.get_active_room_for_user(user_id)
            if active_room and active_room.chat_id != chat_id:
                other_player = active_room.get_player(user_id)
                if other_player and not other_player.is_removed and active_room.state not in [RoomState.CLOSED, RoomState.MATCH_OVER]:
                    return None, "You are already in an active match in another room."
                else:
                    if active_room.state in [RoomState.CLOSED, RoomState.MATCH_OVER]:
                        await self.db.delete_room(active_room.chat_id)

            if room.is_lobby_locked:
                return None, "🔒 The lobby is closed."

            if len(room.all_current_players) >= MAX_PLAYERS:
                return room, f"Room is full (maximum {MAX_PLAYERS} players)."

            # Case: LOBBY
            if room.state == RoomState.LOBBY:
                active = room.active_players
                if existing and existing.is_removed:
                    existing.is_removed = False
                    existing.is_left = False
                    existing.is_waiting = False
                    existing.seat_order = len(active)
                    if last_name:
                        existing.last_name = last_name
                else:
                    new_player = Player(
                        user_id=user_id,
                        room_id=chat_id,
                        username=username,
                        first_name=first_name,
                        last_name=last_name,
                        seat_order=len(active),
                        is_waiting=False,
                    )
                    room.players.append(new_player)
                await self.db.save_room(room)
                display = f"{first_name} {last_name}" if last_name else first_name
                await self.db.add_player_to_room_atomic(chat_id, user_id, display, username)
                # Reload from DB to ensure rendering from database
                room = await self.db.load_room(chat_id)
                return room, "Joined successfully."

            # Case: PLAYING (Mid-match queue as waiting)
            elif room.state == RoomState.PLAYING:
                if existing and existing.is_removed:
                    existing.is_removed = False
                    existing.is_left = False
                    existing.is_waiting = True
                    if last_name:
                        existing.last_name = last_name
                else:
                    new_player = Player(
                        user_id=user_id,
                        room_id=chat_id,
                        username=username,
                        first_name=first_name,
                        last_name=last_name,
                        seat_order=len(room.players),
                        is_waiting=True,
                    )
                    room.players.append(new_player)
                await self.db.save_room(room)
                await self.update_table_message(room)
                return room, "Joined! You're in the waiting queue and will be seated at the start of next round."

            # Case: PAUSED (Fewer than 4 players remain)
            elif room.state == RoomState.PAUSED:
                if existing and existing.is_removed:
                    existing.is_removed = False
                    existing.is_waiting = False
                    existing.seat_order = len(room.active_players)
                    if last_name:
                        existing.last_name = last_name
                else:
                    new_player = Player(
                        user_id=user_id,
                        room_id=chat_id,
                        username=username,
                        first_name=first_name,
                        last_name=last_name,
                        seat_order=len(room.active_players),
                        is_waiting=False,
                    )
                    room.players.append(new_player)

                can_resume = len(room.active_players) >= MIN_PLAYERS
                starter = None
                newly_seated = []
                if can_resume:
                    starter, newly_seated = GameEngine.deal_round(room, room.current_round)
                await self.db.save_room(room)
                await self.update_table_message(room)
                if can_resume and starter:
                    asyncio.create_task(
                        self.send_round_transition_messages(room, starter, newly_seated)
                    )
                    return room, "Joined and seated! Match resumed with fresh redeal."
                return room, "Joined and seated! Waiting for more players to resume match."

            else:
                return room, "Cannot join this room."

    async def leave_room(
        self, chat_id: int, user_id: int, confirmed: bool = False
    ) -> Tuple[Optional[Room], str]:
        """
        Handles player leave in lobby or mid-match.
        """
        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room:
                return None, "You're not in this game."

            player = room.get_player(user_id)
            if not player or player.is_removed:
                return room, "You're not in this game."

            # If player was in waiting queue, remove them directly
            if player.is_waiting:
                room.players = [p for p in room.players if p.user_id != user_id]
                for i, p in enumerate(room.players):
                    p.seat_order = i
                new_host = None
                if room.host_id == user_id:
                    if room.players:
                        new_host = GameEngine.transfer_host(room)
                await self.db.save_room(room)
                if self.bot:
                    try:
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=msgs.get_leave_match_announcement(player),
                            parse_mode="HTML",
                        )
                        if new_host:
                            await self.bot.send_message(
                                chat_id=chat_id,
                                text=msgs.get_host_transferred_announcement(new_host),
                                parse_mode="HTML",
                            )
                    except Exception as e:
                        logger.error("Error announcing leave: %s", e)
                return room, "You left the waiting queue."

            # Lobby leave
            if room.state == RoomState.LOBBY:
                room.players = [p for p in room.players if p.user_id != user_id]
                for i, p in enumerate(room.players):
                    p.seat_order = i

                new_host = None
                if room.host_id == user_id:
                    if room.players:
                        new_host = GameEngine.transfer_host(room)
                    else:
                        room.state = RoomState.CLOSED
                        await self.db.delete_room(chat_id)
                        if self.bot:
                            try:
                                await self.bot.send_message(
                                    chat_id=chat_id,
                                    text=msgs.get_leave_lobby_announcement(player, 0),
                                    parse_mode="HTML",
                                )
                            except Exception as e:
                                logger.error("Error announcing leave: %s", e)
                        return room, "Host left and room closed."

                await self.db.save_room(room)
                count = len(room.all_current_players)
                if self.bot:
                    try:
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=msgs.get_leave_lobby_announcement(player, count),
                            parse_mode="HTML",
                        )
                        if new_host:
                            await self.bot.send_message(
                                chat_id=chat_id,
                                text=msgs.get_host_transferred_announcement(new_host),
                                parse_mode="HTML",
                            )
                    except Exception as e:
                        logger.error("Error announcing leave: %s", e)

                await self.update_lobby_message(chat_id)
                return room, "You left the lobby."

            # Mid-match leave
            if not confirmed:
                return room, "CONFIRM_REQUIRED"

            player.is_left = True
            old_host_id = room.host_id

            round_voided, can_continue = GameEngine.remove_player_and_handle_state(
                room, user_id
            )
            new_host = room.get_player(room.host_id) if old_host_id == user_id and room.host_id else None

            if round_voided and can_continue:
                starter, newly_seated = GameEngine.deal_round(room, room.current_round)

            await self.db.save_room(room)
            self.invalidate_room_tokens(chat_id)

            if self.bot:
                try:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=msgs.get_leave_match_announcement(player),
                        parse_mode="HTML",
                    )
                    if new_host:
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=msgs.get_host_transferred_announcement(new_host),
                            parse_mode="HTML",
                        )
                except Exception as e:
                    logger.error("Error announcing leave: %s", e)

            if round_voided and can_continue:
                await self.update_table_message(room, last_event=f"🚪 {player.display_name} left the game.")
                asyncio.create_task(
                    self.send_round_transition_messages(room, starter, newly_seated)
                )
            elif not can_continue:
                await self.update_table_message(room, last_event="⏸ Match paused (fewer than 4 players).")

            return room, "You left the game."

    async def start_match(
        self, chat_id: int, requester_id: int, sender_chat_id: Optional[int] = None
    ) -> Tuple[Optional[Room], Optional[str], Optional[Player], List[Player]]:
        """
        Starts the match (host or group admin). Deals Round 1 with fair starter & random shuffle.
        """
        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room:
                return None, "Room not found.", None, []

            is_priv = await self.is_privileged_user(chat_id, requester_id, sender_chat_id)
            if not is_priv:
                return room, "Only the host or a group admin can do that.", None, []

            if len(room.active_players) < MIN_PLAYERS:
                return room, f"Need at least {MIN_PLAYERS} players (now {len(room.active_players)}).", None, []

            starter, newly_seated = GameEngine.deal_round(room, round_number=1)
            if room.lobby_message_id and not room.table_message_id:
                room.table_message_id = room.lobby_message_id
            await self.db.save_room(room)
            return room, None, starter, newly_seated

    async def process_pass(
        self,
        chat_id: int,
        passer_id: int,
        card_id: str,
        nonce: int,
    ) -> Tuple[bool, Optional[str], Optional[Card], Optional[Player], Optional[Player]]:
        """
        Processes a manual card pass. Validates turn nonce.
        Sends turn ping to receiver.
        """
        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room:
                return False, "Room not found.", None, None, None

            if room.state != RoomState.PLAYING:
                return False, "Round is not currently active.", None, None, None

            if room.turn_nonce != nonce:
                return False, "Turn already completed or button is stale.", None, None, None

            if room.active_player_id != passer_id:
                return False, "It is not your turn to pass.", None, None, None

            try:
                passed_card, passer, receiver = GameEngine.pass_card(room, passer_id, card_id)
            except ValueError as e:
                return False, str(e), None, None, None

            # Automatic win check: immediately check passer hand (has 4 cards)
            is_win, win_rank, is_match_over, match_winners = GameEngine.check_round_winner_after_pass(room, passer)

            await self.db.save_room(room)
            # Invalidate tokens for previous turn in this room
            self.invalidate_room_tokens(chat_id)

        # Update public table message in group
        await self.update_table_message(
            room, last_event=msgs.get_turn_announcement(passer, receiver)
        )

        if is_win:
            await self._handle_round_win(room, passer, win_rank, is_match_over)
        else:
            # Bot sends its own turn ping tagging the next player
            await self.send_turn_ping(room, receiver)

        return True, None, passed_card, passer, receiver

    async def _handle_round_win(
        self,
        room: Room,
        winner: Player,
        win_rank: Optional[int],
        is_match_over: bool,
    ) -> None:
        round_win_msg = msgs.get_round_win_announcement(room.current_round, winner, win_rank)
        if is_match_over:
            final_score_text = msgs.get_scoreboard_text(room, is_final=True)
            full_end_msg = f"{round_win_msg}\n\n🎉 *MATCH COMPLETED!*\n\n{final_score_text}"
            if self.bot:
                try:
                    await self.bot.send_message(chat_id=room.chat_id, text=full_end_msg, parse_mode="Markdown")
                except Exception as e:
                    logger.error("Error sending match end msg: %s", e)
        else:
            new_room, starter, newly_seated = await self.advance_to_next_round(room.chat_id)
            if new_room and starter:
                asyncio.create_task(
                    self.send_round_transition_messages(
                        new_room, starter, newly_seated, round_win_msg=round_win_msg
                    )
                )

    async def process_autopass(
        self,
        chat_id: int,
    ) -> Tuple[bool, Optional[Player], Optional[Player]]:
        """
        Executes auto-pass when turn deadline expires.
        Sends turn ping to receiver.
        """
        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room or room.state != RoomState.PLAYING:
                return False, None, None

            active_player = room.get_active_player()
            if not active_player:
                return False, None, None

            try:
                passed_card, passer, receiver = GameEngine.auto_pass_card(room)
            except ValueError as e:
                logger.error("Auto-pass error in room %s: %s", chat_id, e)
                return False, None, None

            # Automatic win check: immediately check passer hand (has 4 cards)
            is_win, win_rank, is_match_over, match_winners = GameEngine.check_round_winner_after_pass(room, passer)

            await self.db.save_room(room)
            self.invalidate_room_tokens(chat_id)

        # Update table message
        await self.update_table_message(
            room, last_event=msgs.get_autopass_announcement(passer)
        )

        # Group announcement: "⏱ Auto-passed for [Name]."
        if self.bot:
            try:
                await self.bot.send_message(
                    chat_id=room.chat_id,
                    text=msgs.get_autopass_announcement(passer),
                )
            except Exception as e:
                logger.error("Error sending auto-pass announcement: %s", e)

        if is_win:
            await self._handle_round_win(room, passer, win_rank, is_match_over)
        else:
            # Send bot's turn panel to receiver
            await self.send_turn_panel(room, receiver)

        return True, passer, receiver

    async def process_claim_victory(
        self,
        chat_id: int,
        claimer_id: int,
        nonce: Optional[int] = None,
    ) -> Tuple[bool, Optional[str], Optional[int], bool, List[Player]]:
        """
        Validates claim victory request.
        """
        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room:
                return False, "Room not found.", None, False, []

            if room.state != RoomState.PLAYING:
                return False, "Round is already finished or not active.", None, False, []

            is_valid, rank, err = GameEngine.validate_claim_victory(room, claimer_id)
            if not is_valid:
                return False, err, None, False, []

            is_match_over, match_winners = GameEngine.conclude_round(room, claimer_id)
            await self.db.save_room(room)

            self.invalidate_room_tokens(chat_id)

            return True, None, rank, is_match_over, match_winners

    async def advance_to_next_round(
        self, chat_id: int
    ) -> Tuple[Optional[Room], Optional[Player], List[Player]]:
        """
        Deals the next round with fair starter & random shuffle.
        """
        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room or room.state == RoomState.MATCH_OVER:
                return None, None, []

            next_round = room.current_round + 1
            if next_round > room.total_rounds:
                room.state = RoomState.MATCH_OVER
                await self.db.save_room(room)
                return room, None, []

            starter, newly_seated = GameEngine.deal_round(room, next_round)
            await self.db.save_room(room)
            await self.update_table_message(room)
            return room, starter, newly_seated

    async def set_lobby_lock(
        self,
        chat_id: int,
        requester_id: int,
        lock: bool,
        actor_name: str = "the host",
        sender_chat_id: Optional[int] = None,
    ) -> Tuple[bool, str, Optional[Room]]:
        """
        Host or group admin sets lobby lock (/open or /close).
        """
        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room:
                return False, "Room not found.", None

            is_priv = await self.is_privileged_user(chat_id, requester_id, sender_chat_id)
            if not is_priv:
                return False, "Only the host or a group admin can do that.", room

            if not lock and len(room.all_current_players) >= MAX_PLAYERS:
                return False, f"Lobby is full ({MAX_PLAYERS}/{MAX_PLAYERS}).", room

            room.is_lobby_locked = lock
            await self.db.save_room(room)

            if room.state == RoomState.PLAYING:
                await self.update_table_message(room)

            msg = (
                f"🔒 Lobby closed by {actor_name}."
                if lock
                else f"🔓 Lobby open! Opened by {actor_name}. Use ➕ Join to join. ({len(room.all_current_players)}/{MAX_PLAYERS} players)"
            )
            return True, msg, room

    async def kill_room(
        self,
        chat_id: int,
        requester_id: int,
        sender_chat_id: Optional[int] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Host or group admin kills the current game immediately.
        """
        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room:
                return False, "Room not found.", None

            is_priv = await self.is_privileged_user(chat_id, requester_id, sender_chat_id)
            if not is_priv:
                return False, "Only the host or a group admin can do that.", None

            self.invalidate_room_tokens(chat_id)

            sorted_players = sorted(room.players, key=lambda x: x.score, reverse=True)
            lines = []
            for i, p in enumerate(sorted_players, start=1):
                status = " (removed)" if p.is_removed else ""
                lines.append(f"{i}. {p.mention}: {p.score} pt{'s' if p.score != 1 else ''}{status}")
            scores_text = "\n".join(lines) if lines else "No scores recorded."

            await self.db.delete_room(chat_id)
            return True, "Game ended.", scores_text

    async def remove_player(
        self,
        chat_id: int,
        requester_id: Optional[int] = None,
        target_user_id: int = 0,
        sender_chat_id: Optional[int] = None,
        host_id: Optional[int] = None,
        actor_name: str = "Admin",
    ) -> Tuple[bool, str, bool, bool]:
        """
        Host or group admin removes a player (in lobby or mid-match).
        """
        if requester_id is None:
            requester_id = host_id if host_id is not None else 0

        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room:
                return False, "Room not found.", False, False

            is_priv = await self.is_privileged_user(chat_id, requester_id, sender_chat_id)
            if not is_priv:
                return False, "Only the host or a group admin can do that.", False, False

            target = room.get_player(target_user_id)
            if not target or target.is_removed:
                return False, "Player not found or already removed.", False, False

            if target_user_id == room.host_id:
                return False, "The host cannot be removed.", False, False

            remove_text = msgs.get_removed_announcement(target.display_name, actor_name)

            if room.state == RoomState.LOBBY:
                room.players = [p for p in room.players if p.user_id != target_user_id]
                for i, p in enumerate(room.players):
                    p.seat_order = i
                await self.db.save_room(room)
                self.invalidate_room_tokens(chat_id)
                if self.bot:
                    try:
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=remove_text,
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.error("Error announcing removal: %s", e)
                await self.update_lobby_message(chat_id)
                return True, f"Player {target.display_name} was removed.", False, True

            round_voided, can_continue = GameEngine.remove_player_and_handle_state(
                room, target_user_id
            )

            if round_voided and can_continue:
                starter, newly_seated = GameEngine.deal_round(room, room.current_round)

            await self.db.save_room(room)
            self.invalidate_room_tokens(chat_id)

            if self.bot:
                try:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=remove_text,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error("Error announcing removal: %s", e)

            if round_voided and can_continue:
                await self.update_table_message(room, last_event=f"🚫 {target.display_name} was removed.")
                asyncio.create_task(
                    self.send_round_transition_messages(room, starter, newly_seated)
                )
            elif not can_continue:
                await self.update_table_message(room, last_event="⏸ Match paused (fewer than 4 players).")

            return True, f"Player {target.display_name} was removed.", round_voided, can_continue

    async def process_skip(
        self,
        chat_id: int,
        requester_id: int,
        sender_chat_id: Optional[int] = None,
        actor_name: str = "Admin",
    ) -> Tuple[bool, str, Optional[Player], Optional[Player]]:
        """
        Privileged skip of current player's turn. Passes a random card without inactivity flagging.
        """
        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room:
                return False, "Room not found.", None, None

            is_priv = await self.is_privileged_user(chat_id, requester_id, sender_chat_id)
            if not is_priv:
                return False, "Only the host or a group admin can do that.", None, None

            if room.state != RoomState.PLAYING or not room.get_active_player():
                return False, "There's no active turn to skip.", None, None

            passer = room.get_active_player()
            try:
                passed_card, passer, receiver = GameEngine.skip_turn(room)
            except ValueError as e:
                logger.error("Skip error in room %s: %s", chat_id, e)
                return False, "There's no active turn to skip.", None, None

            # Automatic win check: immediately check passer hand (has 4 cards)
            is_win, win_rank, is_match_over, match_winners = GameEngine.check_round_winner_after_pass(room, passer)

            await self.db.save_room(room)
            self.invalidate_room_tokens(chat_id)

        skip_text = msgs.get_skipped_announcement(passer.display_name, actor_name)
        await self.update_table_message(room, last_event=skip_text)

        if self.bot:
            try:
                await self.bot.send_message(
                    chat_id=room.chat_id,
                    text=skip_text,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("Error sending skip announcement: %s", e)

        if is_win:
            await self._handle_round_win(room, passer, win_rank, is_match_over)
        else:
            await self.send_turn_panel(room, receiver)
        return True, "", passer, receiver

    async def add_player(
        self,
        chat_id: int,
        requester_id: int,
        target_id: int,
        target_first_name: str,
        target_username: Optional[str] = None,
        target_last_name: Optional[str] = None,
        sender_chat_id: Optional[int] = None,
        actor_name: str = "Admin",
    ) -> Tuple[bool, str]:
        """
        Privileged add of a target player into the room.
        Overrides closed lobby; in match, queues player for next round.
        """
        async with self.get_lock(chat_id):
            room = await self.db.load_room(chat_id)
            if not room or room.state in [RoomState.CLOSED, RoomState.MATCH_OVER]:
                return False, "No active room in this group."

            is_priv = await self.is_privileged_user(chat_id, requester_id, sender_chat_id)
            if not is_priv:
                return False, "Only the host or a group admin can do that."

            target_display = f"{target_first_name} {target_last_name}" if target_last_name else target_first_name

            existing = room.get_player(target_id)
            if existing and not existing.is_removed:
                return False, f"{target_display} is already in the game."

            active_room = await self.db.get_active_room_for_user(target_id)
            if active_room and active_room.chat_id != chat_id:
                return False, f"{target_display} is already in another game."

            if len(room.all_current_players) >= MAX_PLAYERS:
                return False, f"Room is full (maximum {MAX_PLAYERS} players)."

            if room.state == RoomState.LOBBY:
                if existing and existing.is_removed:
                    existing.is_removed = False
                    existing.is_left = False
                    existing.is_waiting = False
                    existing.seat_order = len(room.active_players)
                    if target_last_name:
                        existing.last_name = target_last_name
                else:
                    new_player = Player(
                        user_id=target_id,
                        room_id=chat_id,
                        username=target_username,
                        first_name=target_first_name,
                        last_name=target_last_name,
                        seat_order=len(room.active_players),
                        is_waiting=False,
                    )
                    room.players.append(new_player)
                await self.db.save_room(room)
                count = len(room.all_current_players)
                announcement = msgs.get_added_announcement(target_display, actor_name, count)
                if self.bot:
                    try:
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=announcement,
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.error("Error announcing add: %s", e)
                await self.update_lobby_message(chat_id)
                return True, ""
            else:
                # Mid-match: queue as waiting for next round
                if existing and existing.is_removed:
                    existing.is_removed = False
                    existing.is_left = False
                    existing.is_waiting = True
                    existing.seat_order = len(room.players)
                    if target_last_name:
                        existing.last_name = target_last_name
                else:
                    new_player = Player(
                        user_id=target_id,
                        room_id=chat_id,
                        username=target_username,
                        first_name=target_first_name,
                        last_name=target_last_name,
                        seat_order=len(room.players),
                        is_waiting=True,
                    )
                    room.players.append(new_player)
                await self.db.save_room(room)
                count = len(room.all_current_players)
                announcement = msgs.get_added_announcement(target_display, actor_name, count)
                if self.bot:
                    try:
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=announcement,
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.error("Error announcing add: %s", e)
                await self.update_table_message(room, last_event=f"➕ {target_display} joined queue.")
                return True, ""

