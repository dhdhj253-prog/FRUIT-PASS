# BotFather Setup Guide for "Fruit Pass" (@FruitPassBot)

Use the following exact strings in [@BotFather](https://t.me/BotFather) to configure your bot identity.

---

### 1. Bot Name
Send command: `/setname`  
Select your bot and paste:
```text
Fruit Pass
```

---

### 2. Short Description (max 120 chars)
*Displayed on the bot's profile page and in invite previews.*  
Send command: `/setdescription`  
Select your bot and paste:
```text
Fast card-passing game for 4 to 8 players. Collect four matching fruits to win!
```

---

### 3. About / Full Description
*Displayed on the empty chat screen before pressing Start.*  
Send command: `/setabouttext`  
Select your bot and paste:
```text
Everyone starts with 4 fruit cards. On your turn, secretly pass one card to the next player and try to end up with four of the same fruit. Be the first to claim victory and win the round. Play 5, 8, or 10 rounds. Most points wins the match.
```

---

### 4. Bot Commands
Send command: `/setcommands`  
Select your bot and paste the following block:
```text
start - Start the game (host, in a group)
newgame - Create a room and become host
join - Join an open lobby
leave - Leave the current room
open - Open the lobby so players can join (host only)
close - Close the lobby to block new players (host only)
kill - End the current game (host only)
remove - Remove an inactive player (host only)
rules - How to play
score - Show the scoreboard
help - List all commands
```

---

### 5. Tagline & Welcome Message Reference
- **Tagline**: `Pass smart. Match four. Win the round.`
- **Default Welcome Message (`/start`)**:
> 🍎 Welcome to Fruit Pass! Collect four of the same fruit before anyone else. Pass one card each turn and keep your hand secret. Add me to a group and use /newgame to host, or /join to enter a room. You need 4 to 8 players.
