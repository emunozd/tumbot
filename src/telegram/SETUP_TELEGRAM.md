# SETUP_TELEGRAM.md — Complete setup guide for the Telegram bot

## Step 1 — Create the bot with BotFather

1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Give it a name (e.g. "My tumbot")
4. Give it a username (must end in `bot`, e.g. `my_tumbot_private_bot`)
5. BotFather returns a **token** — copy it, you need it in your `.env`

---

## Step 2 — Make the bot invisible (private)

Run these commands in your chat with `@BotFather`:

```
/setprivacy       → select your bot → choose "Enable"
                    (bot cannot read group messages, only direct commands)

/setjoingroups    → select your bot → choose "Disable"
                    (nobody can add the bot to groups)

/setdescription   → select your bot → write something generic or leave it blank
                    (don't hint at what it does)
```

> ⚠️ Do NOT share the bot username with anyone.
> The token is like a password — never commit it to git.

---

## Step 3 — Get your personal chat_id

1. Search for `@userinfobot` on Telegram
2. Send `/start`
3. It replies with your `Id:` — that is your chat_id

> Keep it handy. You can use it to verify that `/vincular` worked correctly.

---

## Step 4 — Configure your .env

Add these two lines to your `.env` file:

```env
TELEGRAM_BOT_TOKEN=123456789:AABBCCDDEEFFaabbccddeeff-yours
TELEGRAM_LINK_SECRET=a-secret-phrase-only-you-know
```

**Rules for the secret:**
- Minimum 12 characters
- Don't use single dictionary words
- Example: `tumbot-xK9#mango-2026`
- This secret never travels over the network — it lives only on your server

---

## Step 5 — Apply the code patches

1. `src/telegram/bot.py` → new file (replaces the previous one if it exists)
2. `src/telegram/__init__.py` → new file if it doesn't exist: `# src/telegram/__init__.py`
3. `src/data/database.py` → apply `database_patch.py`
4. `main.py` → apply `main_patch.py`
5. `docker-compose.yml` → apply `docker_patch.txt`
6. `requirements.txt` → add `python-telegram-bot>=21.0.0`

---

## Step 6 — Rebuild and start

```bash
docker compose down
docker compose build
docker compose up -d
```

---

## Step 7 — Claim the bot as yours

1. Open Telegram and find your bot by its username
2. Send: `/vincular your-secret-phrase-only-you-know`
3. If the secret matches, the bot replies confirming you are now the registered owner
4. From this point on, any other user who writes to the bot receives **complete silence**

---

## Step 8 — Verify everything works

```
/status     → should show the bot's current state
/positions  → open positions (or "no open positions")
/help       → full command list
```

---

## Security flow summary

```
Someone finds your bot
        │
        ▼
They send any command
        │
        ▼
Is their chat_id the owner_chat_id registered in bot_config?
   NO  →  absolute silence (not even "unauthorized", nothing at all)
   YES →  responds normally
```

```
First use (bot has no owner yet):
/vincular <secret>
        │
        ▼
Does it match TELEGRAM_LINK_SECRET in .env?
   NO  →  silence
   YES →  saves chat_id to bot_config.owner_chat_id → confirms ownership
```

---

## Multiple instances

Each tumbot deployment is independent. Every instance needs its own bot token and its own `TELEGRAM_LINK_SECRET`. The setup steps above are identical for each deployment.

---

## Additional notes

- If you lose access to your Telegram account or want to transfer ownership:
  ```bash
  docker exec -it tumbot sqlite3 /data/bot.db "DELETE FROM bot_config WHERE key='owner_chat_id';"
  ```
  Then run `/vincular` again from the new account.

- Unauthorized access attempts are logged in Docker logs:
  ```bash
  docker logs tumbot | grep "Acceso no autorizado"
  ```