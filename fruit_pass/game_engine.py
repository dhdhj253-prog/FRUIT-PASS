import random
import secrets
import time
from typing import List, Optional, Tuple

from fruit_pass.config import (
    MIN_PLAYERS,
    MAX_PLAYERS,
    TURN_TIMEOUT_SECONDS,
    CONSECUTIVE_AUTOPASS_FLAG_THRESHOLD,
    RANK_NAME_BY_INDEX,
    RANK_EMOJI_BY_INDEX,
)
from fruit_pass.models import Room, Player, Card, RoomState


class GameEngine:
    @staticmethod
    def create_deck(num_players: int) -> List[Card]:
        """
        Creates a deck using the first num_players fruit ranks.
        Each rank has exactly 4 physical cards.
        Total cards = 4 * num_players.
        """
        if not (MIN_PLAYERS <= num_players <= MAX_PLAYERS):
            raise ValueError(f"Player count must be between {MIN_PLAYERS} and {MAX_PLAYERS}")

        deck: List[Card] = []
        for rank in range(num_players):
            for copy_idx in range(4):
                deck.append(Card.create(rank, copy_idx))
        return deck

    @staticmethod
    def seat_waiting_players(room: Room) -> List[Player]:
        """
        Seats any waiting players into active play for the new round.
        """
        newly_seated = [p for p in room.players if p.is_waiting and not p.is_removed]
        for p in newly_seated:
            p.is_waiting = False
        return newly_seated

    @staticmethod
    def select_starter_and_shuffle_order(room: Room) -> Player:
        """
        Chooses starting player randomly from players who have not started yet in current cycle.
        Puts starter first, then randomly shuffles remaining players with secure RNG.
        """
        rng = secrets.SystemRandom()
        active = room.active_players
        if not active:
            raise ValueError("No active players")

        candidates = [p for p in active if p.user_id not in room.started_this_cycle]
        if not candidates:
            # All active players have started! Reset cycle
            room.started_this_cycle.clear()
            if room.last_starter_id and len(active) > 1:
                candidates = [p for p in active if p.user_id != room.last_starter_id]
            else:
                candidates = list(active)
        elif len(room.started_this_cycle) == 0 and room.last_starter_id and len(active) > 1:
            # Fresh cycle: avoid picking previous round's starter if > 1 player
            candidates = [p for p in active if p.user_id != room.last_starter_id]

        starter = rng.choice(candidates)

        # Put starter first, shuffle the remaining players
        remaining = [p for p in active if p.user_id != starter.user_id]
        rng.shuffle(remaining)

        new_order = [starter] + remaining
        for idx, p in enumerate(new_order):
            p.seat_order = idx

        other_players = [p for p in room.players if p.user_id not in {x.user_id for x in new_order}]
        room.players = new_order + other_players

        room.active_player_id = starter.user_id
        room.last_starter_id = starter.user_id
        return starter

    @staticmethod
    def deal_round(room: Room, round_number: int) -> Tuple[Player, List[Player]]:
        """
        Seats waiting players, picks a fair random starter, shuffles pass order,
        and deals 4 cards each.
        Returns (starter, newly_seated).
        """
        newly_seated = GameEngine.seat_waiting_players(room)
        active = room.active_players
        num_players = len(active)
        if num_players < MIN_PLAYERS:
            raise ValueError(f"Cannot deal with fewer than {MIN_PLAYERS} players")

        starter = GameEngine.select_starter_and_shuffle_order(room)

        deck = GameEngine.create_deck(num_players)
        rng = secrets.SystemRandom()
        rng.shuffle(deck)

        # Clear existing hands
        for p in room.players:
            p.cards.clear()
            p.last_received_card_id = None

        # Deal 4 cards each in new order
        for i, player in enumerate(room.active_players):
            player.cards = deck[i * 4 : (i + 1) * 4]
            player.sort_hand()

        room.current_round = round_number
        room.state = RoomState.PLAYING
        room.round_pass_count = 0
        room.turn_nonce += 1
        room.turn_deadline = time.time() + TURN_TIMEOUT_SECONDS
        room.turn_notified_stages = ""

        return starter, newly_seated

    @staticmethod
    def pass_card(
        room: Room,
        passer_id: int,
        card_id: str,
    ) -> Tuple[Card, Player, Player]:
        """
        Active player passes card_id to the next player in seating order.
        Returns (passed_card, passer, receiver).
        """
        if room.state != RoomState.PLAYING:
            raise ValueError("Room is not in PLAYING state")

        if room.active_player_id != passer_id:
            raise ValueError("Not your turn to pass")

        passer = room.get_player(passer_id)
        if not passer or passer.is_removed:
            raise ValueError("Invalid passer")

        card_idx = next((i for i, c in enumerate(passer.cards) if c.id == card_id), None)
        if card_idx is None:
            raise ValueError("Card not found in passer's hand")

        receiver = room.next_active_player_after(passer_id)
        if not receiver:
            raise ValueError("No receiver available")

        passed_card = passer.cards.pop(card_idx)
        receiver.cards.append(passed_card)
        receiver.last_received_card_id = passed_card.id

        passer.sort_hand()
        receiver.sort_hand()

        # Reset passer auto-pass counters & flag on successful manual action
        passer.consecutive_autopasses = 0
        passer.is_flagged = False

        # Turn progression
        room.round_pass_count += 1
        room.active_player_id = receiver.user_id
        room.turn_nonce += 1
        room.turn_deadline = time.time() + TURN_TIMEOUT_SECONDS
        room.turn_notified_stages = ""

        return passed_card, passer, receiver

    @staticmethod
    def auto_pass_card(room: Room, count_inactivity: bool = True) -> Tuple[Card, Player, Player]:
        """
        Automatically passes a random card from current active player to next player.
        Flags player if consecutive_autopasses threshold reached.
        Returns (passed_card, passer, receiver).
        """
        if room.state != RoomState.PLAYING:
            raise ValueError("Room is not in PLAYING state")

        passer = room.get_active_player()
        if not passer:
            raise ValueError("No active player found for auto-pass")

        if not passer.cards:
            raise ValueError("Passer has no cards to pass")

        chosen_card = random.choice(passer.cards)
        card_idx = passer.cards.index(chosen_card)

        receiver = room.next_active_player_after(passer.user_id)
        if not receiver:
            raise ValueError("No receiver available")

        passed_card = passer.cards.pop(card_idx)
        receiver.cards.append(passed_card)
        receiver.last_received_card_id = passed_card.id

        passer.sort_hand()
        receiver.sort_hand()

        # Update auto-pass counter and flag check if counting inactivity
        if count_inactivity:
            passer.consecutive_autopasses += 1
            if passer.consecutive_autopasses >= CONSECUTIVE_AUTOPASS_FLAG_THRESHOLD:
                passer.is_flagged = True

        # Turn progression
        room.round_pass_count += 1
        room.active_player_id = receiver.user_id
        room.turn_nonce += 1
        room.turn_deadline = time.time() + TURN_TIMEOUT_SECONDS
        room.turn_notified_stages = ""

        return passed_card, passer, receiver

    @staticmethod
    def skip_turn(room: Room) -> Tuple[Card, Player, Player]:
        """
        Server passes a random card from current player's hand to next player without flagging for inactivity.
        """
        return GameEngine.auto_pass_card(room, count_inactivity=False)

    @staticmethod
    def validate_claim_victory(
        room: Room,
        player_id: int,
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        """
        Validates claim victory request.
        A player can only claim victory if their hand has four cards of the same fruit.
        Returns (is_valid, winning_rank, error_message).
        """
        if room.state != RoomState.PLAYING:
            return False, None, "Round is not currently active."

        player = room.get_player(player_id)
        if not player or player.is_removed:
            return False, None, "Player not found or removed."

        winning_rank = player.has_four_of_a_kind()
        if winning_rank is None:
            return False, None, "❌ That's not four of a kind. Keep playing."

        # Valid claim! Award point
        player.score += 1
        return True, winning_rank, None

    @staticmethod
    def check_round_winner_after_pass(
        room: Room,
        passer: Player,
    ) -> Tuple[bool, Optional[int], bool, List[Player]]:
        """
        Immediately after a pass is committed, checks the hand of the player who just passed.
        If all 4 cards are the same fruit, that player is the winner of the round at once.
        Awards 1 point and concludes the round.
        Returns (is_win, winning_rank, is_match_over, match_winners).
        """
        winning_rank = passer.has_four_of_a_kind()
        if winning_rank is None:
            return False, None, False, []

        passer.score += 1
        is_match_over, match_winners = GameEngine.conclude_round(room, passer.user_id)
        return True, winning_rank, is_match_over, match_winners

    @staticmethod
    def conclude_round(
        room: Room,
        winner_id: int,
    ) -> Tuple[bool, List[Player]]:
        """
        Handles round win.
        Checks if match is over (current_round >= total_rounds).
        Returns (is_match_over, match_winners).
        """
        # Round completed successfully -> consume starter slot for current cycle
        if room.last_starter_id:
            room.started_this_cycle.add(room.last_starter_id)
            if all(p.user_id in room.started_this_cycle for p in room.active_players):
                room.started_this_cycle.clear()

        if room.current_round >= room.total_rounds:
            room.state = RoomState.MATCH_OVER
            room.active_player_id = None
            room.turn_deadline = None
            # Find match winner(s)
            highest_score = max((p.score for p in room.players), default=0)
            winners = [p for p in room.players if p.score == highest_score and highest_score > 0]
            return True, winners
        else:
            # Round is over, waiting to advance to next round
            room.state = RoomState.ROUND_OVER
            room.active_player_id = None
            room.turn_deadline = None
            return False, []

    @staticmethod
    def remove_player_and_handle_state(
        room: Room,
        target_user_id: int,
    ) -> Tuple[bool, bool]:
        """
        Removes player (marking is_removed=True).
        If mid-round (state == PLAYING), voids the round!
        If active players >= MIN_PLAYERS:
            returns (round_voided=True, can_continue=True) -> caller will deal fresh round
        If active players < MIN_PLAYERS:
            room.state = RoomState.PAUSED
            returns (round_voided=True, can_continue=False) -> match paused
        Also checks host transfer if removed player was host.
        """
        target = room.get_player(target_user_id)
        if not target or target.is_removed:
            return False, False

        was_playing = (room.state == RoomState.PLAYING)
        target.is_removed = True
        target.cards.clear()

        # Removed player leaves the starter pool
        room.started_this_cycle.discard(target_user_id)

        # Host transfer check
        if room.host_id == target_user_id:
            GameEngine.transfer_host(room)

        active = room.active_players
        if was_playing:
            # Round is voided mid-round
            if len(active) >= MIN_PLAYERS:
                # Can redeal fresh round for the same current_round
                return True, True
            else:
                room.state = RoomState.PAUSED
                room.active_player_id = None
                room.turn_deadline = None
                return True, False

        return False, len(active) >= MIN_PLAYERS

    @staticmethod
    def transfer_host(room: Room) -> Optional[Player]:
        """
        Transfers host to the earliest-joined remaining player.
        """
        candidates = [p for p in room.players if not p.is_removed and p.user_id != room.host_id]
        if not candidates:
            remaining = [p for p in room.players if not p.is_removed]
            if not remaining:
                room.host_id = 0
                return None
            new_host = remaining[0]
            room.host_id = new_host.user_id
            return new_host
        new_host = min(candidates, key=lambda p: p.seat_order)
        room.host_id = new_host.user_id
        return new_host
