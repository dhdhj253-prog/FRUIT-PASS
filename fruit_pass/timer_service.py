import asyncio
import time
import logging
from typing import Optional
from telegram import Bot
from fruit_pass.models import RoomState
from fruit_pass.database import Database
from fruit_pass.game_service import GameService
from fruit_pass.config import ABANDONED_ROOM_TIMEOUT_SECONDS
import fruit_pass.bot_messages as msgs

logger = logging.getLogger(__name__)


class TimerService:
    def __init__(self, db: Database, game_service: GameService, bot: Optional[Bot] = None):
        self.db = db
        self.game_service = game_service
        self.bot = bot
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_cleanup_time = time.time()

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info("TimerService background loop started.")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("TimerService stopped.")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Exception in TimerService loop: %s", e, exc_info=True)

            await asyncio.sleep(1.0)

    async def tick(self) -> None:
        now = time.time()
        active_rooms = await self.db.list_active_rooms()

        for room in active_rooms:
            if room.state != RoomState.PLAYING or not room.turn_deadline:
                continue

            remaining = int(room.turn_deadline - now)

            # Auto-pass when deadline expires
            if remaining <= 0:
                success, passer, receiver = await self.game_service.process_autopass(room.chat_id)
                if success and passer and receiver:
                    logger.info("Auto-passed in room %s for player %s", room.chat_id, passer.display_name)

        # Periodic cleanup of abandoned rooms (every 5 minutes)
        if now - self._last_cleanup_time > 300:
            self._last_cleanup_time = now
            cleaned = await self.db.cleanup_abandoned_rooms(ABANDONED_ROOM_TIMEOUT_SECONDS)
            if cleaned:
                logger.info("Cleaned up abandoned rooms: %s", cleaned)
