import asyncio
import logging
import os
from dotenv import load_dotenv

load_dotenv()

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    ChatMemberHandler,
)

from fruit_pass.config import BOT_TOKEN, DB_PATH
from fruit_pass.database import Database
from fruit_pass.game_service import GameService
from fruit_pass.timer_service import TimerService
from fruit_pass.bot_handlers import BotHandlers

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def create_bot_app(token: str = BOT_TOKEN, db_path: str = DB_PATH):
    """
    Creates and configures the Application, Database, GameService, and TimerService.
    """
    db = Database(db_path=db_path)
    app = ApplicationBuilder().token(token).build()

    game_service = GameService(db=db, bot=app.bot)
    timer_service = TimerService(db=db, game_service=game_service, bot=app.bot)
    handlers = BotHandlers(game_service=game_service)

    # Register Command Handlers
    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("startgame", handlers.cmd_start))
    app.add_handler(CommandHandler("newgame", handlers.cmd_newgame))
    app.add_handler(CommandHandler("join", handlers.cmd_join))
    app.add_handler(CommandHandler("leave", handlers.cmd_leave))
    app.add_handler(CommandHandler("open", handlers.cmd_open))
    app.add_handler(CommandHandler("close", handlers.cmd_close))
    app.add_handler(CommandHandler("kill", handlers.cmd_kill))
    app.add_handler(CommandHandler("remove", handlers.cmd_remove))
    app.add_handler(CommandHandler("rules", handlers.cmd_rules))
    app.add_handler(CommandHandler("score", handlers.cmd_score))
    app.add_handler(CommandHandler("help", handlers.cmd_help))

    # Register Callback Query Handler
    app.add_handler(CallbackQueryHandler(handlers.handle_callback_query))

    # Register Inline Query and Chosen Inline Result Handlers
    app.add_handler(InlineQueryHandler(handlers.handle_inline_query))
    app.add_handler(ChosenInlineResultHandler(handlers.handle_chosen_inline_result))

    # Register Chat Member Handler for blocked/unblocked status
    app.add_handler(ChatMemberHandler(handlers.handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    # Register Chat Member Handler for group admin rights cache invalidation
    app.add_handler(ChatMemberHandler(handlers.handle_chat_member, ChatMemberHandler.CHAT_MEMBER))

    return app, db, game_service, timer_service


async def post_init(application: Application, db: Database, timer_service: TimerService):
    await db.init_db()
    await db.archive_abandoned_rooms_and_clear_stale_locks()
    timer_service.start()
    logger.info("Database initialized and TimerService started.")


async def post_shutdown(application: Application, timer_service: TimerService):
    timer_service.stop()
    logger.info("TimerService stopped.")


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN is not set! Please set BOT_TOKEN in .env or environment.")
        return

    db_path = os.getenv("DB_PATH", "fruit_pass.db")
    db = Database(db_path=db_path)

    app = ApplicationBuilder().token(token).build()
    game_service = GameService(db=db, bot=app.bot)
    timer_service = TimerService(db=db, game_service=game_service, bot=app.bot)
    handlers = BotHandlers(game_service=game_service)

    # Handlers
    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("startgame", handlers.cmd_start))
    app.add_handler(CommandHandler("newgame", handlers.cmd_newgame))
    app.add_handler(CommandHandler("join", handlers.cmd_join))
    app.add_handler(CommandHandler("leave", handlers.cmd_leave))
    app.add_handler(CommandHandler("open", handlers.cmd_open))
    app.add_handler(CommandHandler("close", handlers.cmd_close))
    app.add_handler(CommandHandler("kill", handlers.cmd_kill))
    app.add_handler(CommandHandler("remove", handlers.cmd_remove))
    app.add_handler(CommandHandler("rules", handlers.cmd_rules))
    app.add_handler(CommandHandler("score", handlers.cmd_score))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CallbackQueryHandler(handlers.handle_callback_query))
    app.add_handler(InlineQueryHandler(handlers.handle_inline_query))
    app.add_handler(ChosenInlineResultHandler(handlers.handle_chosen_inline_result))
    app.add_handler(ChatMemberHandler(handlers.handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handlers.handle_chat_member, ChatMemberHandler.CHAT_MEMBER))

    async def on_startup(app: Application):
        await db.init_db()
        await db.archive_abandoned_rooms_and_clear_stale_locks()
        timer_service.start()

    async def on_shutdown(app: Application):
        timer_service.stop()

    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    logger.info("Starting Fruit Pass Bot polling...")
    app.run_polling(
        allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result", "my_chat_member", "chat_member"]
    )


if __name__ == "__main__":
    main()
