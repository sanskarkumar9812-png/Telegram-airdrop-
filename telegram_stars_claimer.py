"""
Telegram (Telethon) userbot that watches OGU Chat groups for
Stars Airdrop messages and claims them as fast as possible.

SETUP ON RAILWAY
----------------
Variables to set:
  API_ID         = your numeric api_id (from my.telegram.org)
  API_HASH       = your api_hash
  SESSION_STRING = from generate_session.py
  TARGET_CHATS   = chatter,messaging  (default)
  GIFTING_BOT    = gifting            (default)
  MIN_STARS_EACH = 300                (default, only claim 300+ stars)
"""

import logging
import os
import random
import re
from urllib.parse import urlparse, parse_qs

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import StartBotRequest

# ───────────────────────── CONFIG ─────────────────────────
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "your_api_hash_here")
SESSION_STRING = os.environ.get("SESSION_STRING", "")

TARGET_CHATS = [
    c.strip() for c in os.environ.get("TARGET_CHATS", "chatter,messaging").split(",") if c.strip()
]
GIFTING_BOT = os.environ.get("GIFTING_BOT", "gifting")
MIN_STARS_EACH = int(os.environ.get("MIN_STARS_EACH", "300"))
AIRDROP_KEYWORDS = ["airdrop"]
ALREADY_CLAIMED_PHRASES = ["fully claimed", "better luck next time"]
# ──────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stars_claimer")
logging.getLogger("telethon").setLevel(logging.WARNING)

if not SESSION_STRING:
    raise SystemExit("SESSION_STRING is not set. Run generate_session.py first.")

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
gifting_entity = None
attempted_messages = set()


def is_unclaimed_airdrop(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    if not any(k in low for k in AIRDROP_KEYWORDS):
        return False

    stars_match = re.search(r"gifting\s+(\d+)", low)
    if stars_match:
        stars_each = int(stars_match.group(1))
        if stars_each < MIN_STARS_EACH:
            log.info("Skipping airdrop - only %d stars each (minimum is %d)", stars_each, MIN_STARS_EACH)
            return False

    m = re.search(r"claimed:\s*(\d+)\s*/\s*(\d+)", low)
    if m:
        claimed, total = int(m.group(1)), int(m.group(2))
        return claimed < total

    return not any(p in low for p in ALREADY_CLAIMED_PHRASES)


def extract_start_param(url: str):
    try:
        q = parse_qs(urlparse(url).query)
        if "start" in q:
            return q["start"][0]
        if "startattach" in q:
            return q["startattach"][0]
    except Exception:
        pass
    return None


async def fire_start_claim(start_param: str, context: str):
    try:
        result = await client(
            StartBotRequest(
                bot=gifting_entity,
                peer=gifting_entity,
                random_id=random.getrandbits(63),
                start_param=start_param,
            )
        )
        reply_texts = []
        for upd in getattr(result, "updates", []) or []:
            msg = getattr(upd, "message", None)
            text = getattr(msg, "message", None)
            if text:
                reply_texts.append(text)
        if reply_texts:
            log.info("[%s] Claim result: %s", context, " | ".join(reply_texts))
        else:
            log.info("[%s] StartBotRequest sent (start=%s)", context, start_param)
        return result
    except FloodWaitError as e:
        log.warning("Flood wait %ss while claiming", e.seconds)
    except Exception as e:
        log.info("[%s] Claim attempt failed (likely lost the race): %s", context, e)


async def try_claim(message, context: str):
    key = (message.chat_id, message.id)
    if key in attempted_messages:
        return

    if not message.buttons:
        log.info("[%s] Airdrop message has no buttons, skipping", context)
        return

    for row in message.buttons:
        for button in row:
            url = getattr(button, "url", None)

            if url and "t.me" not in url.lower():
                continue

            log.info("[%s] Saw button text=%r url=%r data=%r",
                     context, button.text, url, getattr(button, "data", None))

            if url:
                start_param = extract_start_param(url)
                attempted_messages.add(key)
                if start_param:
                    await fire_start_claim(start_param, context)
                else:
                    log.warning("[%s] t.me URL button had no parseable start param: %s", context, url)
                return

            if getattr(button, "data", None) is not None:
                attempted_messages.add(key)
                try:
                    result = await message.click(button=button)
                    log.info("[%s] Clicked callback button -> %s", context, result)
                except FloodWaitError as e:
                    log.warning("Flood wait %ss while clicking", e.seconds)
                except Exception as e:
                    log.info("[%s] Click failed (likely already claimed): %s", context, e)
                return


@client.on(events.NewMessage(chats=TARGET_CHATS))
async def on_new_message(event):
    msg = event.message
    via_bot_id = getattr(msg, "via_bot_id", None)
    is_from_gifting = via_bot_id == gifting_entity.id if (gifting_entity and via_bot_id) else False
    text_matches = is_unclaimed_airdrop(msg.text)

    if not (is_from_gifting or text_matches):
        return
    if not text_matches:
        return

    log.info("Live airdrop spotted (new message), attempting claim...")
    await try_claim(msg, "new")


@client.on(events.MessageEdited(chats=TARGET_CHATS))
async def on_edited_message(event):
    msg = event.message
    if not is_unclaimed_airdrop(msg.text):
        return
    if not msg.buttons:
        return
    log.info("Live airdrop spotted (edited message), attempting claim...")
    await try_claim(msg, "edited")


async def main():
    global gifting_entity
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("SESSION_STRING is invalid or expired. Regenerate it.")
    me = await client.get_me()
    log.info("Logged in as %s (@%s | id: %s)", me.first_name, me.username or "no username", me.id)

    for chat in TARGET_CHATS:
        await client.get_entity(chat)
    gifting_entity = await client.get_entity(GIFTING_BOT)

    log.info("Watching %s for Stars Airdrops via @%s (min %d stars each) ...",
             ", ".join(f"@{c}" for c in TARGET_CHATS), GIFTING_BOT, MIN_STARS_EACH)
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
