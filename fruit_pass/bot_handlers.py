import asyncio
import html
import logging
import secrets
from typing import Optional, Tuple
from telegram import (
    Update,
    CallbackQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    User,
    MessageEntity,
)
from telegram.error import TelegramError, Forbidden
from telegram.ext import ContextTypes

from fruit_pass.models import RoomState, Room
from fruit_pass.game_service import GameService
from fruit_pass.config import BOT_USERNAME, MIN_PLAYERS, MAX_PLAYERS
import fruit_pass.bot_messages as msgs
import fruit_pass.bot_keyboards as kb

logger = logging.getLogger(__name__)


def _extract_actor(update: Update) -> Tuple[Optional[int], str, str, Optional[int]]:
    """
    Returns (user_id, actor_name, actor_log, sender_chat_id).
    Handles anonymous admin messages (sender_chat.id == chat.id) and normal users/callbacks.
    """
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    msg = getattr(update, "effective_message", None)

    # Anonymous admin message mode (sender_chat.id == chat.id)
    if msg:
        sender_chat = getattr(msg, "sender_chat", None)
        sender_chat_id = getattr(sender_chat, "id", None) if sender_chat else None
        chat_id = getattr(chat, "id", None) if chat else None
        if isinstance(sender_chat_id, int) and isinstance(chat_id, int) and sender_chat_id == chat_id:
            return (None, "a group admin", "anonymous admin", sender_chat_id)

    user_id = getattr(user, "id", None) if user else None
    first_name = getattr(user, "first_name", None) if user else None
    last_name = getattr(user, "last_name", None) if user else None
    first_name_str = first_name if isinstance(first_name, str) and first_name else None
    last_name_str = last_name if isinstance(last_name, str) and last_name else None
    if first_name_str and last_name_str:
        actor_name = f"{first_name_str} {last_name_str}"
    elif first_name_str:
        actor_name = first_name_str
    else:
        actor_name = "a group admin"
    actor_log = str(user_id) if user_id is not None else "unknown"
    return (user_id, actor_name, actor_log, None)


class BotHandlers:
    def __init__(self, game_service: GameService):
        self.game_service = game_service

    async def handle_join(
        self,
        chat_id: int,
        user: User,
        origin: str = "cmd",
        update: Optional[Update] = None,
        query: Optional[CallbackQuery] = None,
    ) -> None:
        try:
            is_active = await self.game_service.check_user_registered_or_probe(
                user.id, user.first_name, user.username
            )
            if not is_active:
                deep_link = msgs.get_start_deep_link(f"join_{chat_id}")
                if query:
                    await query.answer(url=deep_link)
                    return
                if update and update.effective_message:
                    await update.effective_message.reply_text(
                        "👋 Start the bot first to join the game.",
                        reply_markup=kb.get_start_bot_keyboard(deep_link),
                    )
                return

            user_display = f"{user.first_name} {user.last_name}" if getattr(user, "last_name", None) else user.first_name
            room, message = await self.game_service.join_room(
                chat_id=chat_id,
                user_id=user.id,
                first_name=user.first_name,
                username=user.username,
                last_name=user.last_name,
            )

            if not room:
                if origin == "start" and update and update.effective_message:
                    await update.effective_message.reply_text(
                        "That game is no longer open. Ask the host to start a new one."
                    )
                elif query:
                    await query.answer(message, show_alert=True)
                elif update and update.effective_message:
                    await update.effective_message.reply_text(message)
                return

            if message == "You're already in this game.":
                if query:
                    await query.answer("You're already in this game.", show_alert=True)
                elif update and update.effective_message:
                    await update.effective_message.reply_text("You're already in this game.")
                return

            if not (message == "Joined successfully." or message.startswith("Joined")):
                if origin == "start" and update and update.effective_message:
                    await update.effective_message.reply_text(
                        "That game is no longer open. Ask the host to start a new one."
                    )
                elif query:
                    await query.answer(message, show_alert=True)
                elif update and update.effective_message:
                    await update.effective_message.reply_text(message)
                return

            # Successful join:
            # (a) Send in the group: "➕ [Name] has joined the game! ([n]/8 players)"
            total_players = len(room.all_current_players)
            announcement = msgs.get_joined_announcement(user_display, total_players)
            if self.game_service.bot:
                try:
                    await self.game_service.bot.send_message(
                        chat_id=chat_id,
                        text=announcement,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error("Error announcing join in group: %s", e)

            # (b) Update lobby panel in place
            await self.game_service.update_lobby_message(chat_id)

            if query:
                await query.answer("Joined successfully!")
            if origin == "start" and update and update.effective_message:
                await update.effective_message.reply_text(
                    msgs.get_join_start_confirmation_text(), parse_mode="Markdown"
                )

        except Exception as e:
            logger.error("Error handling join for user %s in room %s: %s", user.id, chat_id, e)
            if query:
                try:
                    await query.answer("Error processing join.", show_alert=True)
                except Exception:
                    pass
            elif update and update.effective_message:
                await update.effective_message.reply_text("Error processing join.")

    async def handle_leave(
        self,
        chat_id: int,
        user: User,
        update: Optional[Update] = None,
        query: Optional[CallbackQuery] = None,
    ) -> None:
        try:
            room = await self.game_service.db.load_room(chat_id)
            if not room:
                room = await self.game_service.db.get_active_room_for_user(user.id)
            if not room:
                if query:
                    await query.answer("You're not in this game.", show_alert=True)
                elif update and update.effective_message:
                    await update.effective_message.reply_text("You're not in this game.")
                return

            player = room.get_player(user.id)
            if not player or player.is_removed:
                if query:
                    await query.answer("You're not in this game.", show_alert=True)
                elif update and update.effective_message:
                    await update.effective_message.reply_text("You're not in this game.")
                return

            # In lobby or waiting queue: leave directly
            if room.state == RoomState.LOBBY or player.is_waiting:
                room, message = await self.game_service.leave_room(room.chat_id, user.id)
                if query:
                    await query.answer("You left the game.")
                elif update and update.effective_message:
                    await update.effective_message.reply_text(message)
                return

            # Mid-match active player: ask for confirmation
            text = "Leaving will restart the current round for everyone. Leave? ✅ / ❌"
            reply_markup = kb.get_leave_confirm_keyboard(room.chat_id, user.id)
            if query:
                await query.answer()
                if query.message:
                    await query.message.reply_text(text, reply_markup=reply_markup)
            elif update and update.effective_message:
                await update.effective_message.reply_text(text, reply_markup=reply_markup)

        except Exception as e:
            logger.error("Error handling leave for user %s in room %s: %s", user.id, chat_id, e)
            if query:
                try:
                    await query.answer("Error processing leave.", show_alert=True)
                except Exception:
                    pass
            elif update and update.effective_message:
                await update.effective_message.reply_text("Error processing leave.")

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /start command.
        - In a group chat: starts the match (host or group admin, 4-8 players).
        - In a private chat: registers user, onboarding, and handles deep links (e.g. join_<room_id>).
        """
        user = update.effective_user
        chat = update.effective_chat
        if not user or not update.effective_message:
            return

        # In group / supergroup: /start starts the match (host or group admin, 4-8 players)
        if chat and chat.type in ["group", "supergroup"]:
            user_id, actor_name, actor_log, sender_chat_id = _extract_actor(update)
            room = await self.game_service.db.load_room(chat.id)
            if not room or room.state in [RoomState.CLOSED, RoomState.MATCH_OVER]:
                await update.effective_message.reply_text("No game lobby in this group. Use /newgame to create one.")
                return

            if room.state != RoomState.LOBBY:
                await update.effective_message.reply_text("A game is already in progress.")
                return

            room, err, starter, newly_seated = await self.game_service.start_match(
                chat.id, user_id, sender_chat_id=sender_chat_id
            )
            if err:
                await update.effective_message.reply_text(err)
                return

            logger.info("Privileged action start executed by %s in room %s", actor_log, chat.id)

            # Keep one table message, edited in place if possible
            if room.table_message_id:
                await self.game_service.update_table_message(room)
            else:
                sent = await update.effective_message.reply_text(
                    msgs.get_table_text(room),
                    reply_markup=kb.get_table_keyboard(room),
                    parse_mode="Markdown",
                )
                if sent and hasattr(sent, "message_id") and str(sent.message_id).isdigit():
                    room.table_message_id = int(sent.message_id)
                await self.game_service.db.save_room(room)

            if starter:
                asyncio.create_task(
                    self.game_service.send_round_transition_messages(room, starter, newly_seated)
                )
            return

        # Register or activate user in private chat
        await self.game_service.db.register_or_activate_user(
            user.id, user.first_name, user.username
        )

        args = context.args or []
        payload = args[0] if args else ""

        if payload.startswith("join_"):
            room_id_str = payload[5:]
            try:
                room_id = int(room_id_str)
            except ValueError:
                room_id = None

            if room_id is None:
                await update.effective_message.reply_text(
                    "That game is no longer open. Ask the host to start a new one."
                )
                return

            await self.handle_join(room_id, user, origin="start", update=update)
            return

        await update.effective_message.reply_text(msgs.get_welcome_text(), parse_mode="Markdown")

    async def cmd_newgame(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or not user or not update.effective_message:
            return

        if chat.type == "private":
            await update.effective_message.reply_text("🍎 /newgame must be used in a Telegram group chat!")
            return

        is_active = await self.game_service.check_user_registered_or_probe(user.id, user.first_name, user.username)
        if not is_active:
            deep_link = msgs.get_start_deep_link("newgame")
            await update.effective_message.reply_text(
                "👋 Start the bot first to join the game.",
                reply_markup=kb.get_start_bot_keyboard(deep_link),
            )
            return

        # If a lobby or match exists in that group, reply and don't create second room
        existing = await self.game_service.db.load_room(chat.id)
        if existing and existing.state not in [RoomState.CLOSED, RoomState.MATCH_OVER]:
            await update.effective_message.reply_text(
                "A game is already in progress. The host can use /kill to end it first."
            )
            return

        room, created_new = await self.game_service.create_room(
            chat_id=chat.id,
            host_id=user.id,
            host_first_name=user.first_name,
            host_username=user.username,
            host_last_name=user.last_name,
        )

        if not created_new:
            if room.chat_id != chat.id:
                await update.effective_message.reply_text(
                    "You are already in an active room in another group! You can only be in one game at a time."
                )
                return
            await update.effective_message.reply_text(
                "A game is already in progress. The host can use /kill to end it first."
            )
            return

        sent_msg = await update.effective_message.reply_text(
            msgs.get_lobby_text(room),
            reply_markup=kb.get_lobby_keyboard(room, BOT_USERNAME),
            parse_mode="Markdown",
        )
        if sent_msg and hasattr(sent_msg, "message_id") and str(sent_msg.message_id).isdigit():
            room.lobby_message_id = int(sent_msg.message_id)
        await self.game_service.db.save_room(room)

    async def cmd_join(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or not user or not update.effective_message:
            return

        if chat.type == "private":
            await update.effective_message.reply_text("Join via the group chat where the game was created.")
            return

        await self.handle_join(chat.id, user, origin="cmd", update=update)

    async def cmd_leave(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or not user or not update.effective_message:
            return

        await self.handle_leave(chat.id, user, update=update)

    async def cmd_open(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not chat or not update.effective_message:
            return

        user_id, actor_name, actor_log, sender_chat_id = _extract_actor(update)
        room = await self.game_service.db.load_room(chat.id)
        if not room or room.state in [RoomState.CLOSED, RoomState.MATCH_OVER]:
            await update.effective_message.reply_text("No active room in this group.")
            return

        is_priv = await self.game_service.is_privileged_user(chat.id, user_id, sender_chat_id)
        if not is_priv:
            await update.effective_message.reply_text("Only the host or a group admin can do that.")
            return

        logger.info("Privileged action open executed by %s in room %s", actor_log, chat.id)
        success, msg_str, _ = await self.game_service.set_lobby_lock(
            chat.id, user_id, lock=False, actor_name=actor_name, sender_chat_id=sender_chat_id
        )
        if msg_str:
            await update.effective_message.reply_text(msg_str)
        await self.game_service.update_lobby_message(chat.id)

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not chat or not update.effective_message:
            return

        user_id, actor_name, actor_log, sender_chat_id = _extract_actor(update)
        room = await self.game_service.db.load_room(chat.id)
        if not room or room.state in [RoomState.CLOSED, RoomState.MATCH_OVER]:
            await update.effective_message.reply_text("No active room in this group.")
            return

        is_priv = await self.game_service.is_privileged_user(chat.id, user_id, sender_chat_id)
        if not is_priv:
            await update.effective_message.reply_text("Only the host or a group admin can do that.")
            return

        logger.info("Privileged action close executed by %s in room %s", actor_log, chat.id)
        success, msg_str, _ = await self.game_service.set_lobby_lock(
            chat.id, user_id, lock=True, actor_name=actor_name, sender_chat_id=sender_chat_id
        )
        if msg_str:
            await update.effective_message.reply_text(msg_str)
        await self.game_service.update_lobby_message(chat.id)

    async def cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not chat or not update.effective_message:
            return

        user_id, actor_name, actor_log, sender_chat_id = _extract_actor(update)
        room = await self.game_service.db.load_room(chat.id)
        if not room or room.state in [RoomState.CLOSED, RoomState.MATCH_OVER]:
            await update.effective_message.reply_text("No active room in this group.")
            return

        is_priv = await self.game_service.is_privileged_user(chat.id, user_id, sender_chat_id)
        if not is_priv:
            await update.effective_message.reply_text("Only the host or a group admin can do that.")
            return

        logger.info("Privileged action kill executed by %s in room %s", actor_log, chat.id)
        await update.effective_message.reply_text(
            msgs.get_kill_confirm_text(),
            reply_markup=kb.get_kill_confirmation_keyboard(chat.id),
        )

    async def cmd_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not chat or not update.effective_message:
            return

        user_id, actor_name, actor_log, sender_chat_id = _extract_actor(update)
        room = await self.game_service.db.load_room(chat.id)
        if not room or room.state in [RoomState.CLOSED, RoomState.MATCH_OVER]:
            await update.effective_message.reply_text("No active room in this group.")
            return

        is_priv = await self.game_service.is_privileged_user(chat.id, user_id, sender_chat_id)
        if not is_priv:
            await update.effective_message.reply_text("Only the host or a group admin can do that.")
            return

        removable = [p for p in room.players if p.user_id != room.host_id and not p.is_removed]
        if not removable:
            await update.effective_message.reply_text("No other players to remove.")
            return

        logger.info("Privileged action remove executed by %s in room %s", actor_log, chat.id)
        await update.effective_message.reply_text(
            "Select a player to remove:",
            reply_markup=kb.get_remove_list_keyboard(room),
        )

    async def cmd_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not chat or not update.effective_message:
            return

        user_id, actor_name, actor_log, sender_chat_id = _extract_actor(update)
        room = await self.game_service.db.load_room(chat.id)
        if not room or room.state in [RoomState.CLOSED, RoomState.MATCH_OVER]:
            await update.effective_message.reply_text("No active room in this group.")
            return

        is_priv = await self.game_service.is_privileged_user(chat.id, user_id, sender_chat_id)
        if not is_priv:
            await update.effective_message.reply_text("Only the host or a group admin can do that.")
            return

        logger.info("Privileged action skip executed by %s in room %s", actor_log, chat.id)
        success, err_msg, passer, receiver = await self.game_service.process_skip(
            chat.id, user_id, sender_chat_id=sender_chat_id, actor_name=actor_name
        )
        if not success:
            await update.effective_message.reply_text(err_msg or "There's no active turn to skip.")

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not chat or not update.effective_message:
            return

        user_id, actor_name, actor_log, sender_chat_id = _extract_actor(update)
        room = await self.game_service.db.load_room(chat.id)
        if not room or room.state in [RoomState.CLOSED, RoomState.MATCH_OVER]:
            await update.effective_message.reply_text("No active room in this group.")
            return

        is_priv = await self.game_service.is_privileged_user(chat.id, user_id, sender_chat_id)
        if not is_priv:
            await update.effective_message.reply_text("Only the host or a group admin can do that.")
            return

        msg = update.effective_message
        target_user = None

        # 1. Reply to message with /add
        if msg.reply_to_message and msg.reply_to_message.from_user:
            target_user = msg.reply_to_message.from_user

        # 2. Text mention in message entities
        if not target_user and msg.entities:
            for entity in msg.entities:
                if entity.type == MessageEntity.TEXT_MENTION and entity.user:
                    target_user = entity.user
                    break

        target_id = None
        target_first_name = None
        target_username = None
        target_last_name = None

        # 3. /add @username
        if not target_user and context.args:
            username_arg = context.args[0].lstrip("@")
            user_row = await self.game_service.db.get_user_by_username(username_arg)
            if user_row:
                target_id = user_row["user_id"]
                target_first_name = user_row["first_name"]
                target_username = user_row.get("username")
                target_last_name = user_row.get("last_name")

        if target_user:
            if target_user.is_bot:
                await msg.reply_text("Bots can't be added.")
                return
            target_id = target_user.id
            target_first_name = target_user.first_name
            target_username = target_user.username
            target_last_name = getattr(target_user, "last_name", None)

        if not target_id:
            await msg.reply_text(
                "I couldn't find that person. Reply to their message with /add, or ask them to start the bot first."
            )
            return

        target_display = f"{target_first_name} {target_last_name}" if target_last_name else target_first_name

        # Check registration gate
        is_active = await self.game_service.check_user_registered_or_probe(
            target_id, target_first_name, target_username
        )
        if not is_active:
            deep_link = msgs.get_start_deep_link(f"join_{chat.id}")
            await msg.reply_text(
                f"{target_display} needs to start the bot first.",
                reply_markup=kb.get_start_bot_keyboard(deep_link),
            )
            return

        success, err_msg = await self.game_service.add_player(
            chat_id=chat.id,
            requester_id=user_id,
            target_id=target_id,
            target_first_name=target_first_name,
            target_username=target_username,
            target_last_name=target_last_name,
            sender_chat_id=sender_chat_id,
            actor_name=actor_name,
        )
        if not success:
            await msg.reply_text(err_msg)

    async def cmd_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(msgs.get_rules_text(), parse_mode="Markdown")

    async def cmd_score(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not chat or not update.effective_message:
            return
        room = await self.game_service.db.load_room(chat.id)
        if not room or room.state == RoomState.CLOSED:
            await update.effective_message.reply_text("No active game room in this chat.")
            return

        await update.effective_message.reply_text(msgs.get_scoreboard_text(room), parse_mode="Markdown")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        help_text = (
            "🍎 *Fruit Pass Commands:*\n"
            "/start - Start the game (host, in a group)\n"
            "/startgame - Alias to start the game\n"
            "/newgame - Create a room and become host\n"
            "/join - Join an open lobby\n"
            "/leave - Leave the game\n"
            "/add - Add a player (host or admin). Reply to their message with /add\n"
            "/remove - Remove a player (host or admin)\n"
            "/skip - Skip the current player's turn (host or admin)\n"
            "/open - Open the lobby so players can join (host only)\n"
            "/close - Close the lobby to block new players (host only)\n"
            "/kill - End the current game (host only)\n"
            "/rules - How to play\n"
            "/score - Show the scoreboard\n"
            "/help - List all commands"
        )
        if update.effective_message:
            await update.effective_message.reply_text(help_text, parse_mode="Markdown")

    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return

        data = query.data
        user = query.from_user
        parts = data.split(":")
        action = parts[0]
        room_id: Optional[int] = None
        if len(parts) > 1 and parts[1].lstrip("-").isdigit():
            room_id = int(parts[1])

        user_id, actor_name, actor_log, sender_chat_id = _extract_actor(update)

        try:
            if action == "noop":
                await query.answer()
                return

            # Join room via group button
            if action == "join":
                await self.handle_join(room_id, user, origin="callback", query=query)
                return

            # Lobby round setting
            if action == "rnd":
                chosen_rounds = int(parts[2])
                async with self.game_service.get_lock(room_id):
                    room = await self.game_service.db.load_room(room_id)
                    if not room or room.state != RoomState.LOBBY:
                        await query.answer()
                        return
                    is_priv = await self.game_service.is_privileged_user(room_id, user.id, sender_chat_id)
                    if not is_priv:
                        await query.answer("Only the host or a group admin can do that.", show_alert=True)
                        return
                    room.total_rounds = chosen_rounds
                    await self.game_service.db.save_room(room)

                logger.info("Privileged action rnd_%s executed by %s in room %s", chosen_rounds, actor_log, room_id)
                await query.answer(f"Rounds: {chosen_rounds}")
                await self.game_service.update_lobby_message(room_id)
                return

            # Start game
            if action == "start":
                is_priv = await self.game_service.is_privileged_user(room_id, user.id, sender_chat_id)
                if not is_priv:
                    await query.answer("Only the host or a group admin can do that.", show_alert=True)
                    return

                room, err, starter, newly_seated = await self.game_service.start_match(
                    room_id, user.id, sender_chat_id=sender_chat_id
                )
                if err:
                    await query.answer(err, show_alert=True)
                    return

                logger.info("Privileged action start executed by %s in room %s", actor_log, room_id)
                await query.answer("Starting game!")
                if query.message:
                    room.table_message_id = query.message.message_id
                    await self.game_service.db.save_room(room)

                try:
                    await query.edit_message_text(
                        msgs.get_table_text(room),
                        reply_markup=kb.get_table_keyboard(room),
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.warning("Error editing lobby to table in room %s: %s", room_id, e)

                if starter:
                    asyncio.create_task(
                        self.game_service.send_round_transition_messages(room, starter, newly_seated)
                    )
                return

            # Leave
            if action == "leave":
                await self.handle_leave(room_id, user, query=query)
                return

            if action == "lvcnf":
                target_user_id = int(parts[2]) if len(parts) > 2 else user.id
                if user.id != target_user_id:
                    await query.answer("This confirmation is not for you.", show_alert=True)
                    return
                await query.answer()
                room, msg_text = await self.game_service.leave_room(room_id, user.id, confirmed=True)
                user_display = f"{user.first_name} {user.last_name}" if getattr(user, "last_name", None) else user.first_name
                await query.edit_message_text(f"🚪 {user_display} confirmed leaving the game.")
                return

            if action == "lvcan":
                target_user_id = int(parts[2]) if len(parts) > 2 else user.id
                if user.id != target_user_id:
                    await query.answer("This confirmation is not for you.", show_alert=True)
                    return
                await query.answer("Cancelled.")
                await query.edit_message_text("Leaving cancelled.")
                return

            # Host/Admin room kill confirmation
            if action == "killcnf":
                is_priv = await self.game_service.is_privileged_user(room_id, user.id, sender_chat_id)
                if not is_priv:
                    await query.answer("Only the host or a group admin can do that.", show_alert=True)
                    return

                success, msg_str, scores_str = await self.game_service.kill_room(
                    room_id, user.id, sender_chat_id=sender_chat_id
                )
                if not success:
                    await query.answer(msg_str, show_alert=True)
                    return

                logger.info("Privileged action kill_confirm executed by %s in room %s", actor_log, room_id)
                await query.answer()
                await query.edit_message_text(
                    msgs.get_kill_announcement(scores_str or "No scores recorded.", actor_name=actor_name)
                )
                return

            if action == "killcan":
                is_priv = await self.game_service.is_privileged_user(room_id, user.id, sender_chat_id)
                if not is_priv:
                    await query.answer("Only the host or a group admin can do that.", show_alert=True)
                    return

                logger.info("Privileged action kill_cancel executed by %s in room %s", actor_log, room_id)
                await query.answer()
                await query.edit_message_text("Game termination cancelled.")
                return

            # Host/Admin remove player
            if action == "rmsel":
                is_priv = await self.game_service.is_privileged_user(room_id, user.id, sender_chat_id)
                if not is_priv:
                    await query.answer("Only the host or a group admin can do that.", show_alert=True)
                    return

                target_user_id = int(parts[2])
                room = await self.game_service.db.load_room(room_id)
                target = room.get_player(target_user_id) if room else None
                name = target.display_name if target else "player"
                logger.info("Privileged action remove_select executed by %s in room %s", actor_log, room_id)
                await query.answer()
                await query.edit_message_text(
                    f"Remove {name}?",
                    reply_markup=kb.get_remove_confirm_keyboard(room_id, target_user_id),
                )
                return

            if action == "rmcnf":
                is_priv = await self.game_service.is_privileged_user(room_id, user.id, sender_chat_id)
                if not is_priv:
                    await query.answer("Only the host or a group admin can do that.", show_alert=True)
                    return

                target_user_id = int(parts[2])
                room_before = await self.game_service.db.load_room(room_id)
                target = room_before.get_player(target_user_id) if room_before else None
                target_name = target.display_name if target else "Player"

                success, msg_str, round_voided, can_continue = await self.game_service.remove_player(
                    room_id, user.id, target_user_id, sender_chat_id=sender_chat_id, actor_name=actor_name
                )
                if not success:
                    await query.answer(msg_str, show_alert=True)
                    return

                logger.info("Privileged action remove_confirm executed by %s in room %s", actor_log, room_id)
                await query.answer()
                await query.edit_message_text(f"🚫 {target_name} was removed by {actor_name}.")
                return

            if action == "rmcan":
                is_priv = await self.game_service.is_privileged_user(room_id, user.id, sender_chat_id)
                if not is_priv:
                    await query.answer("Only the host or a group admin can do that.", show_alert=True)
                    return

                logger.info("Privileged action remove_cancel executed by %s in room %s", actor_log, room_id)
                await query.answer()
                await query.edit_message_text("Player removal cancelled.")
                return

        except TelegramError as e:
            err_str = str(e).lower()
            if any(s in err_str for s in ["message is not modified", "query is too old", "query id is invalid"]):
                logger.debug("Quietly handled callback error for room_id=%s user_id=%s: %s", room_id, user.id, e)
                return
            if "message to edit not found" in err_str or "message_id_invalid" in err_str:
                logger.info("Message to edit not found in room %s, recovering...", room_id)
                if room_id:
                    await self.game_service.update_lobby_message(room_id)
                return
            logger.exception("Unexpected TelegramError in callback handler room_id=%s user_id=%s: %s", room_id, user.id, e)
            try:
                await query.answer("Something went wrong, please try again", show_alert=True)
            except Exception:
                pass
        except Exception as e:
            logger.exception("Unexpected exception in callback handler room_id=%s user_id=%s: %s", room_id, user.id, e)
            try:
                await query.answer("Something went wrong, please try again", show_alert=True)
            except Exception:
                pass

    async def handle_inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handles inline queries.
        - Ignores query text.
        - Identifies user by from_user.id.
        - Resolves active room from user id.
        - Answers with is_personal=True, cache_time=0.
        """
        query = update.inline_query
        if not query:
            return

        user = query.from_user
        room = await self.game_service.db.get_active_room_for_user(user.id)

        # If user is not in an active match
        if not room or room.state != RoomState.PLAYING:
            results = [
                InlineQueryResultArticle(
                    id=f"nomatch_{secrets.token_hex(8)}",
                    title="You're not in a match.",
                    description="Join or start a game in a group first.",
                    input_message_content=InputTextMessageContent(
                        "I'm not currently in an active Fruit Pass match."
                    ),
                )
            ]
            await query.answer(results=results, is_personal=True, cache_time=0)
            return

        player = room.get_player(user.id)
        if not player or player.is_removed:
            results = [
                InlineQueryResultArticle(
                    id=f"nomatch_{secrets.token_hex(8)}",
                    title="You're not in a match.",
                    description="Join or start a game in a group first.",
                    input_message_content=InputTextMessageContent(
                        "I'm not currently in an active Fruit Pass match."
                    ),
                )
            ]
            await query.answer(results=results, is_personal=True, cache_time=0)
            return

        results = []

        is_my_turn = (room.active_player_id == player.user_id)
        sorted_cards = sorted(player.cards, key=lambda c: (c.rank, c.id))

        if not is_my_turn:
            # View-only hand results
            for card in sorted_cards:
                is_new = (card.id == player.last_received_card_id)
                card_title = f"{card.emoji} {card.name}{' 🆕' if is_new else ''}"
                v_token = f"v_{room.turn_nonce}_{secrets.token_hex(8)}"
                self.game_service.register_inline_token(
                    v_token,
                    {
                        "type": "view",
                        "chat_id": room.chat_id,
                        "user_id": player.user_id,
                        "nonce": room.turn_nonce,
                    },
                )
                results.append(
                    InlineQueryResultArticle(
                        id=v_token,
                        title=card_title,
                        description="Not your turn.",
                        input_message_content=InputTextMessageContent(
                            f"👀 {html.escape(player.display_name)} checked their cards.",
                            parse_mode="HTML",
                        ),
                    )
                )
        else:
            # Pass hand results (one per card, 5 cards held)
            receiver = room.next_active_player_after(player.user_id)
            receiver_name = receiver.display_name if receiver else "next player"
            for card in sorted_cards:
                is_new = (card.id == player.last_received_card_id)
                card_title = f"{card.emoji} {card.name}{' 🆕' if is_new else ''}"
                p_token = f"p_{room.turn_nonce}_{secrets.token_hex(8)}"
                self.game_service.register_inline_token(
                    p_token,
                    {
                        "type": "pass",
                        "chat_id": room.chat_id,
                        "user_id": player.user_id,
                        "nonce": room.turn_nonce,
                        "card_id": card.id,
                    },
                )
                pass_announcement = msgs.get_turn_announcement(player, receiver)
                results.append(
                    InlineQueryResultArticle(
                        id=p_token,
                        title=card_title,
                        description=f"Tap to pass this card to {receiver_name}",
                        input_message_content=InputTextMessageContent(
                            pass_announcement,
                            parse_mode="HTML",
                        ),
                    )
                )

        await query.answer(results=results, is_personal=True, cache_time=0)

    async def handle_chosen_inline_result(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handles chosen_inline_result.
        - Verifies token validity, active player, turn nonce, card in hand.
        - Executes pass or claim.
        - Stale/replayed tokens or auto-passed turns are safely ignored.
        """
        cir = update.chosen_inline_result
        if not cir:
            return

        token = cir.result_id
        user = cir.from_user
        token_data = self.game_service.get_inline_token(token)
        if not token_data:
            # Token not recognized or already invalidated
            return

        action_type = token_data.get("type")
        chat_id = token_data.get("chat_id")
        nonce = token_data.get("nonce")

        if action_type == "view":
            # View-only selection: does nothing in game
            return

        if action_type == "pass":
            if not chat_id or nonce is None:
                return
            card_id = token_data.get("card_id")
            if not card_id:
                return

            # Execute pass: validates turn nonce, active player, and card ownership
            success, err, passed_card, passer, receiver = await self.game_service.process_pass(
                chat_id=chat_id,
                passer_id=user.id,
                card_id=card_id,
                nonce=nonce,
            )
            if not success:
                logger.info("Inline pass ignored/rejected: %s", err)
                return

    async def handle_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Subscribes to my_chat_member updates to track when a user blocks/unblocks the bot.
        """
        if not update.my_chat_member:
            return
        chat = update.my_chat_member.chat
        if chat.type != "private":
            return
        new_member = update.my_chat_member.new_chat_member
        if not new_member:
            return

        status = new_member.status
        user_id = chat.id
        if status in ["kicked", "left"]:
            await self.game_service.db.set_user_status(user_id, "blocked")
            logger.info("User %s blocked the bot.", user_id)
        elif status in ["member", "administrator"]:
            from_user = update.my_chat_member.from_user
            if from_user:
                await self.game_service.db.register_or_activate_user(
                    user_id, from_user.first_name, from_user.username
                )
            else:
                await self.game_service.db.set_user_status(user_id, "active")
            logger.info("User %s unblocked/restarted the bot.", user_id)

    async def handle_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Invalidates cached admin status for the user when a chat_member update occurs.
        """
        if not update.chat_member or not update.chat_member.chat:
            return
        chat_id = update.chat_member.chat.id
        user_id = (
            update.chat_member.new_chat_member.user.id
            if update.chat_member.new_chat_member and update.chat_member.new_chat_member.user
            else None
        )
        self.game_service.invalidate_admin_cache(chat_id, user_id)
        logger.info("Invalidated admin cache for user %s in chat %s due to chat_member update.", user_id, chat_id)
