from __future__ import annotations
import enum
from typing import List, Optional, Set
from pydantic import BaseModel, Field

from fruit_pass.config import (
    RANK_NAME_BY_INDEX,
    RANK_EMOJI_BY_INDEX,
    DEFAULT_TOTAL_ROUNDS,
)


class RoomState(str, enum.Enum):
    LOBBY = "LOBBY"
    PLAYING = "PLAYING"
    ROUND_OVER = "ROUND_OVER"
    PAUSED = "PAUSED"
    MATCH_OVER = "MATCH_OVER"
    CLOSED = "CLOSED"


class RegisteredUser(BaseModel):
    user_id: int
    first_name: str
    username: Optional[str] = None
    first_started_at: float
    status: str = "active"


class Card(BaseModel):
    id: str  # Unique physical card id, e.g. "c_0_1"
    rank: int  # 0 to 7
    name: str
    emoji: str

    @classmethod
    def create(cls, rank: int, copy_index: int) -> Card:
        name = RANK_NAME_BY_INDEX[rank]
        emoji = RANK_EMOJI_BY_INDEX[rank]
        return cls(
            id=f"c_{rank}_{copy_index}",
            rank=rank,
            name=name,
            emoji=emoji,
        )

    @property
    def display_str(self) -> str:
        return f"{self.emoji} {self.name}"


class Player(BaseModel):
    user_id: int
    room_id: int
    username: Optional[str] = None
    first_name: str
    last_name: Optional[str] = None
    seat_order: int = 0
    score: int = 0
    consecutive_autopasses: int = 0
    is_flagged: bool = False
    is_removed: bool = False
    is_left: bool = False
    is_waiting: bool = False
    hand_message_id: Optional[int] = None
    last_received_card_id: Optional[str] = None
    cards: List[Card] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        if getattr(self, "last_name", None) and isinstance(self.last_name, str):
            return f"{self.first_name} {self.last_name}"
        return self.first_name

    @property
    def mention(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.display_name

    def sort_hand(self) -> None:
        self.cards.sort(key=lambda c: (c.rank, c.id))

    def has_four_of_a_kind(self) -> Optional[int]:
        """
        Returns rank if hand has 4 of a kind and total cards == 4, else None.
        """
        if len(self.cards) != 4:
            return None
        first_rank = self.cards[0].rank
        if all(c.rank == first_rank for c in self.cards):
            return first_rank
        return None


class Room(BaseModel):
    chat_id: int
    host_id: int
    state: RoomState = RoomState.LOBBY
    total_rounds: int = DEFAULT_TOTAL_ROUNDS
    current_round: int = 0
    active_player_id: Optional[int] = None
    turn_nonce: int = 0
    turn_deadline: Optional[float] = None
    turn_notified_stages: str = ""  # comma-separated notification stages e.g. "60,30"
    starting_player_offset: int = 0
    round_pass_count: int = 0
    table_message_id: Optional[int] = None
    lobby_message_id: Optional[int] = None
    last_ping_message_id: Optional[int] = None
    is_lobby_locked: bool = False
    started_this_cycle: Set[int] = Field(default_factory=set)
    last_starter_id: Optional[int] = None
    last_activity_at: float = 0.0
    players: List[Player] = Field(default_factory=list)

    @property
    def active_players(self) -> List[Player]:
        """Returns non-removed, non-waiting players in seat order."""
        return [
            p for p in sorted(self.players, key=lambda x: x.seat_order)
            if not p.is_removed and not p.is_waiting
        ]

    @property
    def waiting_players(self) -> List[Player]:
        """Returns non-removed players who joined mid-match and wait for next round."""
        return [p for p in self.players if not p.is_removed and p.is_waiting]

    @property
    def all_current_players(self) -> List[Player]:
        """Returns all non-removed players (active + waiting)."""
        return [p for p in self.players if not p.is_removed]

    def get_player(self, user_id: int) -> Optional[Player]:
        for p in self.players:
            if p.user_id == user_id:
                return p
        return None

    def get_active_player(self) -> Optional[Player]:
        if self.active_player_id is None:
            return None
        return self.get_player(self.active_player_id)

    def next_active_player_after(self, current_user_id: int) -> Optional[Player]:
        active = self.active_players
        if not active:
            return None
        for i, p in enumerate(active):
            if p.user_id == current_user_id:
                return active[(i + 1) % len(active)]
        return active[0]
