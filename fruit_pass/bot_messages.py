import html
from typing import List, Optional
from fruit_pass.models import Room, Player
from fruit_pass.config import (
    WELCOME_MESSAGE,
    ABOUT_TEXT,
    SHORT_DESCRIPTION,
    TAGLINE,
    RANK_EMOJI_BY_INDEX,
    RANK_NAME_BY_INDEX,
    CLEAN_BOT_USERNAME,
)


def get_start_deep_link(payload: str) -> str:
    return f"https://t.me/{CLEAN_BOT_USERNAME}?start={payload}"


def get_welcome_text() -> str:
    return "🍎 Welcome to Fruit Pass! Add me to a group and use /newgame to host."


def get_rules_summary_text() -> str:
    return (
        "📖 *How to Play Fruit Pass:*\n"
        "• Be the first to collect four cards of the same fruit!\n"
        "• All card picking is secret and inline: tap *'🎴 My cards'* on the table to see your hand or pass a card.\n"
        "• You have 60 seconds per turn.\n"
        "• When you have four of a kind, tap *'🏆 CLAIM VICTORY'* to win the round!"
    )


def get_join_start_confirmation_text() -> str:
    return (
        "✅ You're all set! Going back to the group to join the game.\n\n"
        + get_rules_summary_text()
    )


def get_player_display_name(player: Player) -> str:
    """
    Returns Telegram display name (first name plus last name if present).
    """
    if hasattr(player, "display_name"):
        return player.display_name
    first = getattr(player, "first_name", "")
    last = getattr(player, "last_name", None)
    if isinstance(last, str) and last:
        return f"{first} {last}"
    return str(first)


def get_player_mention(player: Player) -> str:
    """
    Mention player by Telegram display name, not @username.
    HTML link <a href="tg://user?id=USER_ID">Display Name</a> notifies player even without username.
    """
    name = get_player_display_name(player)
    escaped_name = html.escape(name)
    return f'<a href="tg://user?id={player.user_id}">{escaped_name}</a>'


def get_turn_panel_text(player: Player) -> str:
    """
    Turn panel text, exactly one line: "⏩ Next player: [mention of Name B]".
    Mention by display name, no extra text, no timer, no card/fruit info.
    """
    mention = get_player_mention(player)
    return f"⏩ Next player: {mention}"


def get_turn_ping_message(player: Player) -> str:
    """
    Replaced by turn panel message. Kept as alias for compatibility.
    """
    return get_turn_panel_text(player)


def get_shuffling_text(round_num: int) -> str:
    return f"🔀 Shuffling players for Round {round_num}..."


def get_player_joined_round_text(player: Player) -> str:
    return f"➕ {get_player_display_name(player)} joins from this round."


def get_new_pass_order_text(players: List[Player]) -> str:
    names = [get_player_display_name(p) for p in players]
    if not names:
        return ""
    order_str = " → ".join(names) + f" → {names[0]}"
    return f"🎲 New pass order: {order_str}"


def get_round_started_text(round_num: int, starter: Optional[Player] = None) -> str:
    return f"✅ Round {round_num} has now started!"


def get_rules_text() -> str:
    return (
        f"📖 *Fruit Pass — Rules*\n\n"
        f"• *Players*: 4 to 8 players per game.\n"
        f"• *Objective*: Be the first to collect 4 cards of the same fruit!\n"
        f"• *Ranks*: 🍎 Apple, 🥭 Mango, 🍇 Grape, 🍒 Cherry, 🍋 Lemon, 🍑 Peach, 🍓 Berry, 🍈 Melon.\n"
        f"• *Setup*: Each round uses N fruit ranks for N players (16 to 32 cards). "
        f"Pass order and starting player are randomly and fairly shuffled every round.\n"
        f"• *Turns*: Tap *'🎴 My cards'* on the table message to view your cards via Telegram Inline Query. "
        f"On your turn, tap an unwanted card to pass it directly to the next player. You have 60s per turn.\n"
        f"• *Passing is Secret*: Card identities are never revealed to other players.\n"
        f"• *Claim Victory*: If you collect 4-of-a-kind, select *'🏆 Claim victory'* in the inline picker "
        f"or tap the public *'🏆 CLAIM VICTORY'* button on the table message! Server validates all claims.\n"
        f"• *Scoring*: 1 point per round win. Most points after 5, 8, or 10 rounds wins the match!"
    )


def get_lobby_text(room: Room) -> str:
    active = room.active_players
    waiting = room.waiting_players
    total_count = len(room.all_current_players)
    status_tag = "🔒 Closed" if room.is_lobby_locked else "🔓 Open"

    lines = [
        f"🍎 *Fruit Pass — Game Lobby* ({status_tag})",
        f"🎯 Rounds: *{room.total_rounds}*",
        f"👥 Players: *{total_count}/8*",
        "",
        "*Players:*",
    ]
    for i, p in enumerate(active, start=1):
        host_tag = " (Host)" if p.user_id == room.host_id else ""
        flag_tag = " ⚠️" if p.is_flagged else ""
        lines.append(f"{i}. {p.mention}{host_tag}{flag_tag}")

    if waiting:
        lines.append("")
        lines.append("*Waiting for next round:*")
        for p in waiting:
            lines.append(f"• {p.mention}")

    lines.append("")
    if len(active) < 4:
        lines.append(f"⏳ Waiting for players (Need at least 4 to start)...")
    else:
        lines.append(f"✅ Ready! Host can start the game.")

    return "\n".join(lines)


def get_table_text(room: Room, last_event: Optional[str] = None) -> str:
    """
    Renders the in-place group table message for active matches.
    ANTI-CHEAT: Never contains fruit names or emojis of cards in play!
    """
    active = room.active_players
    status_tag = "🔒 Closed" if room.is_lobby_locked else "🔓 Open"
    total_count = len(room.all_current_players)

    lines = [
        f"🍎 *Fruit Pass — Round {room.current_round}/{room.total_rounds}*",
        f"🚪 Lobby: *{status_tag}* ({total_count}/8 players)",
        f"🎯 Passing is clockwise (First to collect 4-of-a-kind wins)",
        "",
        "*Seating Order:*",
    ]
    for i, p in enumerate(active, start=1):
        turn_tag = " 👈 *(Passing)*" if p.user_id == room.active_player_id else ""
        flag_tag = " ⚠️" if p.is_flagged else ""
        lines.append(f"{i}. {get_player_display_name(p)}{turn_tag}{flag_tag}")

    waiting = room.waiting_players
    if waiting:
        lines.append("")
        lines.append(f"⏳ *Queued for next round ({len(waiting)}):* " + ", ".join(get_player_display_name(p) for p in waiting))

    active_p = room.get_active_player()
    lines.append("")
    if active_p:
        lines.append(f"⏳ Current Turn: *{get_player_display_name(active_p)}* (60s)")

    if last_event:
        lines.append(f"\n📢 {last_event}")

    lines.append("\n_Tap '🎴 My cards' below to open your hand or pass._")
    return "\n".join(lines)


def get_turn_announcement(passer: Player, receiver: Player) -> str:
    """
    ANTI-CHEAT: NEVER reveal the card rank or emoji in group announcements!
    Text: "🎴 [Name A] passed a card to [Name B]"
    Uses Telegram display names, HTML-escaped. No @usernames, no pings, no card information.
    """
    passer_name = html.escape(get_player_display_name(passer))
    receiver_name = html.escape(get_player_display_name(receiver))
    return f"🎴 {passer_name} passed a card to {receiver_name}"


def get_autopass_announcement(passer: Player) -> str:
    """
    ANTI-CHEAT: Never show card identity on auto-pass.
    Uses Telegram display name.
    """
    name = html.escape(get_player_display_name(passer))
    return f"⏱ Auto-passed for {name}."


def get_round_win_announcement(round_num: int, winner: Player, rank: int) -> str:
    emoji = RANK_EMOJI_BY_INDEX.get(rank, "🍎")
    name = RANK_NAME_BY_INDEX.get(rank, "Fruit")
    plural_name = f"{name}s" if not name.endswith("y") else f"{name[:-1]}ies"
    if name == "Peach":
        plural_name = "Peaches"
    elif name == "Mango":
        plural_name = "Mangos"
    winner_name = html.escape(get_player_display_name(winner))
    return f"🏆 {winner_name} wins Round {round_num} with four {emoji} {plural_name}!"


def get_scoreboard_text(room: Room, is_final: bool = False) -> str:
    title = "🏆 *FINAL SCORES*" if is_final else f"📊 *Scoreboard (Round {room.current_round}/{room.total_rounds})*"
    lines = [title, ""]
    # Sort all players by score descending
    sorted_players = sorted(room.players, key=lambda x: x.score, reverse=True)
    for i, p in enumerate(sorted_players, start=1):
        status = " (removed)" if p.is_removed else (" ⚠️" if p.is_flagged else "")
        host_tag = " (Host)" if p.user_id == room.host_id else ""
        lines.append(f"{i}. {get_player_display_name(p)}: *{p.score} pt{'s' if p.score != 1 else ''}*{status}{host_tag}")

    if is_final:
        highest = max((p.score for p in room.players), default=0)
        winners = [p for p in room.players if p.score == highest and highest > 0]
        lines.append("")
        if len(winners) == 1:
            lines.append(f"🎉 *Winner: {get_player_display_name(winners[0])} with {highest} points!*")
        elif len(winners) > 1:
            co_winners = ", ".join(get_player_display_name(w) for w in winners)
            lines.append(f"🎉 *Co-Winners: {co_winners} with {highest} points!*")
        else:
            lines.append("No winner crowned.")

    return "\n".join(lines)


def get_kill_confirm_text() -> str:
    return "⚠️ End the current game for everyone?"


def get_kill_announcement(scores_text: str, actor_name: str = "the host") -> str:
    return f"💀 The game was ended by {actor_name}. Scores so far:\n\n{scores_text}\n\nUse /newgame to start a new one."
