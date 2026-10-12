import aiosqlite
import time
from typing import List, Optional
from fruit_pass.models import Room, Player, Card, RoomState
from fruit_pass.config import DB_PATH


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    async def init_db(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS rooms (
                    chat_id INTEGER PRIMARY KEY,
                    host_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    total_rounds INTEGER NOT NULL,
                    current_round INTEGER NOT NULL,
                    active_player_id INTEGER,
                    turn_nonce INTEGER NOT NULL DEFAULT 0,
                    turn_deadline REAL,
                    turn_notified_stages TEXT NOT NULL DEFAULT '',
                    starting_player_offset INTEGER NOT NULL DEFAULT 0,
                    round_pass_count INTEGER NOT NULL DEFAULT 0,
                    table_message_id INTEGER,
                    last_activity_at REAL NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            try:
                await db.execute("ALTER TABLE rooms ADD COLUMN table_message_id INTEGER;")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE rooms ADD COLUMN lobby_message_id INTEGER;")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE rooms ADD COLUMN last_ping_message_id INTEGER;")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE rooms ADD COLUMN is_lobby_locked INTEGER NOT NULL DEFAULT 0;")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE rooms ADD COLUMN started_this_cycle TEXT NOT NULL DEFAULT '';")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE rooms ADD COLUMN last_starter_id INTEGER;")
            except Exception:
                pass

            await db.execute("""
                CREATE TABLE IF NOT EXISTS room_players (
                    room_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    username TEXT,
                    joined_at REAL NOT NULL,
                    PRIMARY KEY (room_id, user_id),
                    FOREIGN KEY (room_id) REFERENCES rooms (chat_id) ON DELETE CASCADE
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    room_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    seat_order INTEGER NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    consecutive_autopasses INTEGER NOT NULL DEFAULT 0,
                    is_flagged INTEGER NOT NULL DEFAULT 0,
                    is_removed INTEGER NOT NULL DEFAULT 0,
                    is_waiting INTEGER NOT NULL DEFAULT 0,
                    hand_message_id INTEGER,
                    last_received_card_id TEXT,
                    PRIMARY KEY (room_id, user_id),
                    FOREIGN KEY (room_id) REFERENCES rooms (chat_id) ON DELETE CASCADE
                )
            """)
            try:
                await db.execute("ALTER TABLE players ADD COLUMN last_received_card_id TEXT;")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE players ADD COLUMN is_waiting INTEGER NOT NULL DEFAULT 0;")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE players ADD COLUMN last_name TEXT;")
            except Exception:
                pass

            await db.execute("""
                CREATE TABLE IF NOT EXISTS cards (
                    id TEXT NOT NULL,
                    room_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    rank INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    PRIMARY KEY (id, room_id),
                    FOREIGN KEY (room_id) REFERENCES rooms (chat_id) ON DELETE CASCADE
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL,
                    username TEXT,
                    first_started_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                )
            """)
            await db.commit()

    async def save_room(self, room: Room) -> None:
        if room.last_activity_at == 0.0:
            room.last_activity_at = time.time()
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN TRANSACTION")
            try:
                started_cycle_str = ",".join(str(uid) for uid in room.started_this_cycle)
                await db.execute(
                    """
                    INSERT INTO rooms (
                        chat_id, host_id, state, total_rounds, current_round,
                        active_player_id, turn_nonce, turn_deadline,
                        turn_notified_stages, starting_player_offset,
                        round_pass_count, table_message_id, lobby_message_id,
                        last_ping_message_id, is_lobby_locked, started_this_cycle,
                        last_starter_id, last_activity_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        host_id = excluded.host_id,
                        state = excluded.state,
                        total_rounds = excluded.total_rounds,
                        current_round = excluded.current_round,
                        active_player_id = excluded.active_player_id,
                        turn_nonce = excluded.turn_nonce,
                        turn_deadline = excluded.turn_deadline,
                        turn_notified_stages = excluded.turn_notified_stages,
                        starting_player_offset = excluded.starting_player_offset,
                        round_pass_count = excluded.round_pass_count,
                        table_message_id = excluded.table_message_id,
                        lobby_message_id = excluded.lobby_message_id,
                        last_ping_message_id = excluded.last_ping_message_id,
                        is_lobby_locked = excluded.is_lobby_locked,
                        started_this_cycle = excluded.started_this_cycle,
                        last_starter_id = excluded.last_starter_id,
                        last_activity_at = excluded.last_activity_at
                    """,
                    (
                        room.chat_id,
                        room.host_id,
                        room.state.value,
                        room.total_rounds,
                        room.current_round,
                        room.active_player_id,
                        room.turn_nonce,
                        room.turn_deadline,
                        room.turn_notified_stages,
                        room.starting_player_offset,
                        room.round_pass_count,
                        room.table_message_id,
                        room.lobby_message_id,
                        room.last_ping_message_id,
                        1 if room.is_lobby_locked else 0,
                        started_cycle_str,
                        room.last_starter_id,
                        room.last_activity_at,
                        now,
                    ),
                )

                # Prune players not in current room.players
                current_user_ids = [p.user_id for p in room.players]
                if current_user_ids:
                    placeholders = ",".join("?" for _ in current_user_ids)
                    await db.execute(
                        f"DELETE FROM players WHERE room_id = ? AND user_id NOT IN ({placeholders})",
                        (room.chat_id, *current_user_ids),
                    )
                    await db.execute(
                        f"DELETE FROM room_players WHERE room_id = ? AND user_id NOT IN ({placeholders})",
                        (room.chat_id, *current_user_ids),
                    )
                else:
                    await db.execute("DELETE FROM players WHERE room_id = ?", (room.chat_id,))
                    await db.execute("DELETE FROM room_players WHERE room_id = ?", (room.chat_id,))

                # Upsert players & room_players
                for p in room.players:
                    await db.execute(
                        """
                        INSERT INTO players (
                            room_id, user_id, username, first_name, last_name, seat_order,
                            score, consecutive_autopasses, is_flagged, is_removed,
                            is_waiting, hand_message_id, last_received_card_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(room_id, user_id) DO UPDATE SET
                            username = excluded.username,
                            first_name = excluded.first_name,
                            last_name = excluded.last_name,
                            seat_order = excluded.seat_order,
                            score = excluded.score,
                            consecutive_autopasses = excluded.consecutive_autopasses,
                            is_flagged = excluded.is_flagged,
                            is_removed = excluded.is_removed,
                            is_waiting = excluded.is_waiting,
                            hand_message_id = excluded.hand_message_id,
                            last_received_card_id = excluded.last_received_card_id
                        """,
                        (
                            room.chat_id,
                            p.user_id,
                            p.username,
                            p.first_name,
                            p.last_name,
                            p.seat_order,
                            p.score,
                            p.consecutive_autopasses,
                            1 if p.is_flagged else 0,
                            1 if p.is_removed else 0,
                            1 if p.is_waiting else 0,
                            p.hand_message_id,
                            p.last_received_card_id,
                        ),
                    )
                    await db.execute(
                        """
                        INSERT INTO room_players (room_id, user_id, name, username, joined_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(room_id, user_id) DO UPDATE SET
                            name = excluded.name,
                            username = excluded.username
                        """,
                        (room.chat_id, p.user_id, p.display_name, p.username, now),
                    )

                # Replace cards for this room
                await db.execute("DELETE FROM cards WHERE room_id = ?", (room.chat_id,))
                card_tuples = []
                for p in room.players:
                    for c in p.cards:
                        card_tuples.append((
                            c.id,
                            room.chat_id,
                            p.user_id,
                            c.rank,
                            c.name,
                            c.emoji,
                        ))
                if card_tuples:
                    await db.executemany(
                        """
                        INSERT INTO cards (id, room_id, player_id, rank, name, emoji)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        card_tuples,
                    )

                await db.commit()
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def load_room(self, chat_id: int) -> Optional[Room]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM rooms WHERE chat_id = ?", (chat_id,)) as cursor:
                room_row = await cursor.fetchone()
                if not room_row:
                    return None

            # Load players
            players: List[Player] = []
            async with db.execute(
                "SELECT * FROM players WHERE room_id = ? ORDER BY seat_order ASC", (chat_id,)
            ) as cursor:
                player_rows = await cursor.fetchall()
                for prow in player_rows:
                    p = Player(
                        user_id=prow["user_id"],
                        room_id=prow["room_id"],
                        username=prow["username"],
                        first_name=prow["first_name"],
                        last_name=prow["last_name"] if "last_name" in prow.keys() else None,
                        seat_order=prow["seat_order"],
                        score=prow["score"],
                        consecutive_autopasses=prow["consecutive_autopasses"],
                        is_flagged=bool(prow["is_flagged"]),
                        is_removed=bool(prow["is_removed"]),
                        is_waiting=bool(prow["is_waiting"]) if "is_waiting" in prow.keys() else False,
                        hand_message_id=prow["hand_message_id"],
                        last_received_card_id=prow["last_received_card_id"] if "last_received_card_id" in prow.keys() else None,
                        cards=[],
                    )
                    players.append(p)

            # Load cards
            async with db.execute(
                "SELECT * FROM cards WHERE room_id = ?", (chat_id,)
            ) as cursor:
                card_rows = await cursor.fetchall()
                cards_by_player: dict[int, List[Card]] = {}
                for crow in card_rows:
                    pid = crow["player_id"]
                    c = Card(
                        id=crow["id"],
                        rank=crow["rank"],
                        name=crow["name"],
                        emoji=crow["emoji"],
                    )
                    cards_by_player.setdefault(pid, []).append(c)

            for p in players:
                p.cards = cards_by_player.get(p.user_id, [])
                p.sort_hand()

            started_cycle_raw = room_row["started_this_cycle"] if "started_this_cycle" in room_row.keys() and room_row["started_this_cycle"] else ""
            started_cycle_list = [int(x) for x in started_cycle_raw.split(",") if x.strip().isdigit()]

            room = Room(
                chat_id=room_row["chat_id"],
                host_id=room_row["host_id"],
                state=RoomState(room_row["state"]),
                total_rounds=room_row["total_rounds"],
                current_round=room_row["current_round"],
                active_player_id=room_row["active_player_id"],
                turn_nonce=room_row["turn_nonce"],
                turn_deadline=room_row["turn_deadline"],
                turn_notified_stages=room_row["turn_notified_stages"] or "",
                starting_player_offset=room_row["starting_player_offset"],
                round_pass_count=room_row["round_pass_count"],
                table_message_id=room_row["table_message_id"] if "table_message_id" in room_row.keys() else None,
                lobby_message_id=room_row["lobby_message_id"] if "lobby_message_id" in room_row.keys() else None,
                last_ping_message_id=room_row["last_ping_message_id"] if "last_ping_message_id" in room_row.keys() else None,
                is_lobby_locked=bool(room_row["is_lobby_locked"]) if "is_lobby_locked" in room_row.keys() else False,
                started_this_cycle=set(started_cycle_list),
                last_starter_id=room_row["last_starter_id"] if "last_starter_id" in room_row.keys() else None,
                last_activity_at=room_row["last_activity_at"],
                players=players,
            )
            return room

    async def get_active_room_for_user(self, user_id: int) -> Optional[Room]:
        """
        Finds the active room that user_id is currently participating in.
        Returns None if user is not in any active room.
        Ignores CLOSED and MATCH_OVER rooms.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT rooms.chat_id
                FROM rooms
                JOIN players ON rooms.chat_id = players.room_id
                WHERE players.user_id = ?
                  AND players.is_removed = 0
                  AND rooms.state NOT IN (?, ?)
                LIMIT 1
                """,
                (user_id, RoomState.CLOSED.value, RoomState.MATCH_OVER.value),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                chat_id = row["chat_id"]
        return await self.load_room(chat_id)

    async def delete_room(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.execute("DELETE FROM cards WHERE room_id = ?", (chat_id,))
            await db.execute("DELETE FROM players WHERE room_id = ?", (chat_id,))
            await db.execute("DELETE FROM room_players WHERE room_id = ?", (chat_id,))
            await db.execute("DELETE FROM rooms WHERE chat_id = ?", (chat_id,))
            await db.commit()

    async def add_player_to_room_atomic(
        self, room_id: int, user_id: int, name: str, username: Optional[str] = None
    ) -> bool:
        """
        Atomically inserts player into room_players.
        Returns True if newly inserted, False if already exists.
        """
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    """
                    INSERT INTO room_players (room_id, user_id, name, username, joined_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (room_id, user_id, name, username, now),
                )
                await db.commit()
                return True
            except Exception:
                return False

    async def get_room_players(self, room_id: int) -> List[dict]:
        """
        Loads all players directly from room_players table.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM room_players WHERE room_id = ? ORDER BY joined_at ASC", (room_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def archive_abandoned_rooms_and_clear_stale_locks(self) -> int:
        """
        On bot startup, archive abandoned/ended rooms and clear stale locks.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT chat_id FROM rooms WHERE state IN (?, ?)",
                (RoomState.MATCH_OVER.value, RoomState.CLOSED.value),
            ) as cursor:
                rows = await cursor.fetchall()
                ids = [r[0] for r in rows]

            for cid in ids:
                await db.execute("DELETE FROM cards WHERE room_id = ?", (cid,))
                await db.execute("DELETE FROM players WHERE room_id = ?", (cid,))
                await db.execute("DELETE FROM room_players WHERE room_id = ?", (cid,))
                await db.execute("DELETE FROM rooms WHERE chat_id = ?", (cid,))
            await db.commit()
            return len(ids)

    async def list_active_rooms(self) -> List[Room]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT chat_id FROM rooms WHERE state != ?", (RoomState.CLOSED.value,)
            ) as cursor:
                rows = await cursor.fetchall()
                chat_ids = [row["chat_id"] for row in rows]

        rooms = []
        for cid in chat_ids:
            room = await self.load_room(cid)
            if room:
                rooms.append(room)
        return rooms

    async def cleanup_abandoned_rooms(self, max_idle_seconds: float) -> List[int]:
        cutoff = time.time() - max_idle_seconds
        closed_ids = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT chat_id FROM rooms WHERE last_activity_at < ? AND state != ?",
                (cutoff, RoomState.CLOSED.value),
            ) as cursor:
                rows = await cursor.fetchall()
                closed_ids = [row["chat_id"] for row in rows]

        for cid in closed_ids:
            await self.delete_room(cid)
        return closed_ids

    async def register_or_activate_user(
        self, user_id: int, first_name: str, username: Optional[str] = None
    ) -> None:
        """
        Registers user when they press /start in private chat or reactivates them.
        """
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, first_name, username, first_started_at, status)
                VALUES (?, ?, ?, ?, 'active')
                ON CONFLICT(user_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    username = excluded.username,
                    status = 'active'
                """,
                (user_id, first_name, username, now),
            )
            await db.commit()

    async def set_user_status(self, user_id: int, status: str) -> None:
        """
        Updates user status to 'active' or 'blocked'.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
            if row:
                await db.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
            else:
                await db.execute(
                    """
                    INSERT INTO users (user_id, first_name, username, first_started_at, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, "User", None, time.time(), status),
                )
            await db.commit()

    async def is_user_registered_and_active(self, user_id: int) -> bool:
        """
        Returns True if user pressed Start and is not blocked.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return False
                return row["status"] == "active"

    async def get_user(self, user_id: int) -> Optional[dict]:
        """
        Retrieves user record if registered.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return dict(row)
