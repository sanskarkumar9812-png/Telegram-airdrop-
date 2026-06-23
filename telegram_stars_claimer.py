"""
Telegram (Telethon) userbot that watches the OGU Chat group (@chatter) for
"Stars Airdrop" messages posted via the @gifting bot, and claims them as
fast as possible.

HOW THE CLAIM ACTUALLY WORKS (from your screenshots)
------------------------------------------------------
The airdrop message in the group has a "Claim Airdrop" button that is a
DEEP-LINK URL button (e.g. https://t.me/gifting?start=XXXX), not a normal
callback button. Tapping it in a real Telegram client just opens a DM with
@gifting and silently sends "/start XXXX".

So the fastest path is: the instant the airdrop message appears, read the
button's URL straight off the message, pull out the "start=" parameter, and
fire a StartBotRequest directly via the API - the same thing Telegram does
when you tap "START" on a deep link, just without any UI round-trip.

RUNNING THIS ON RAILWAY (or any host with no persistent disk / no way to
type a login code interactively)
-----------------------------------------------------------------------
1. On your OWN computer first: pip install telethon, then run
   generate_session.py once. Log in with your phone/code/2FA when prompted.
   It prints a long "session string" - copy it.
2. On Railway, create a new project from this code (e.g. push to a GitHub
   repo and deploy from there, or use the Railway CLI).
3. In Railway's project settings -> Variables, add:
       API_ID          = your numeric api_id
       API_HASH        = your api_hash
       SESSION_STRING  = the string generate_session.py printed
       TARGET_CHAT     = chatter      (optional, this is the default)
       GIFTING_BOT     = gifting      (optional, this is the default)
4. Set the start command to:  python telegram_stars_claimer.py
   (Railway auto-detects this from requirements.txt + Procfile already
   included alongside this script.)
5. Deploy. Railway keeps the process running continuously and restarts it
   automatically if it ever crashes.

You do not need to log in again on Railway - it logs in using
SESSION_STRING instead of a phone number/code.
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
# All of these can be set as environment variables (recommended for Railway)
# or you can hardcode them directly below for local testing.
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "your_api_hash_here")
SESSION_STRING = os.environ.get("SESSION_STRING", "")  # from generate_session.py

TARGET_CHAT = os.environ.get("TARGET_CHAT", "chatter")   # OGU Chat group, without the @
GIFTING_BOT = os.environ.get("GIFTING_BOT", "gifting")   # the airdrop bot, without the @

# Text that marks a message as a claimable airdrop
AIRDROP_KEYWORDS = ["airdrop"]
# If this phrase is present, the airdrop is already gone - don't bother trying
ALREADY_CLAIMED_PHRASES = ["fully claimed", "better luck next time"]
# ────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stars_claimer")

if not SESSION_STRING:
    raise SystemExit(
        "SESSION_STRING is not set. Run generate_session.py on your own "
        "computer first, then set SESSION_STRING as an environment variable."
    )

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

gifting_entity = None  # resolved at startup


def is_unclaimed_airdrop(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    if not any(k in low for k in AIRDROP_KEYWORDS):
        return False

    # Confirmed format from the live screenshot: "Claimed: 0/1"
    # Use this as the authoritative signal when present.
    m = re.search(r"claimed:\s*(\d+)\s*/\s*(\d+)", low)
    if m:
        claimed, total = int(m.group(1)), int(m.group(2))
        return claimed < total

    # Fallback if the counter line isn't there for some reason
    return not any(p in low for p in ALREADY_CLAIMED_PHRASES)


def extract_start_param(url: str):
    """Pull the `start` (or `startattach`) query param out of a t.me deep link."""
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
        log.info("[%s] Fired StartBotRequest(start=%s) -> ok", context, start_param)
        return result
    except FloodWaitError as e:
        log.warning("Flood wait %ss while claiming", e.seconds)
    except Exception as e:
        log.info("[%s] Claim attempt failed (likely lost the race): %s", context, e)


async def try_claim(message, context: str):
    if not message.buttons:
        log.info("[%s] Airdrop message has no buttons, skipping", context)
        return

    for row in message.buttons:
        for button in row:
            url = getattr(button, "url", None)
            log.info("[%s] Saw button text=%r url=%r data=%r",
                      context, button.text, url, getattr(button, "data", None))

            if url:
                start_param = extract_start_param(url)
                if start_param:
                    await fire_start_claim(start_param, context)
                    return
                else:
                    # No start= param we could parse - log it so we can adjust
                    log.warning("[%s] URL button had no parseable start param: %s", context, url)
                    return

            # Fallback: looks like a normal callback button instead
            if getattr(button, "data", None) is not None:
                try:
                    result = await message.click(button=button)
                    log.info("[%s] Clicked callback button -> %s", context, result)
                except FloodWaitError as e:
                    log.warning("Flood wait %ss while clicking", e.seconds)
                except Exception as e:
                    log.info("[%s] Click failed (likely already claimed): %s", context, e)
                return


@client.on(events.NewMessage(chats=TARGET_CHAT))
async def on_new_message(event):
    msg = event.message
    via_bot_id = getattr(msg, "via_bot_id", None)

    is_from_gifting = via_bot_id == gifting_entity.id if (gifting_entity and via_bot_id) else False
    text_matches = is_unclaimed_airdrop(msg.text)

    if not (is_from_gifting or text_matches):
        return
    if not text_matches:
        return  # came via gifting but text doesn't look like a live airdrop

    log.info("Live airdrop spotted (new message), attempting claim...")
    await try_claim(msg, "new")


@client.on(events.MessageEdited(chats=TARGET_CHAT))
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
        raise SystemExit(
            "SESSION_STRING is invalid or expired. Regenerate it by running "
            "generate_session.py again on your own computer."
        )
    me = await client.get_me()
    log.info("Logged in as %s (@%s)", me.first_name, me.username)

    await client.get_entity(TARGET_CHAT)
    gifting_entity = await client.get_entity(GIFTING_BOT)

    log.info("Watching @%s for Stars Airdrops via @%s ...", TARGET_CHAT, GIFTING_BOT)
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
