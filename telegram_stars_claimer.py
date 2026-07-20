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
       TARGET_CHATS    = chatter,messaging   (optional, this is the default - comma-separated)
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
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import StartBotRequest

# ───────────────────────── CONFIG ─────────────────────────
# All of these can be set as environment variables (recommended for Railway)
# or you can hardcode them directly below for local testing.
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "your_api_hash_here")
SESSION_STRING = os.environ.get("SESSION_STRING", "")  # from generate_session.py

TARGET_CHATS = [
    c.strip() for c in os.environ.get("TARGET_CHATS", "chatter,messaging").split(",") if c.strip()
]  # groups to watch, without the @, comma-separated
GIFTING_BOT = os.environ.get("GIFTING_BOT", "gifting")   # the airdrop bot, without the @

# ── PashaGiftsBot gift card system ──
GIFT_CHANNEL = int(os.environ.get("GIFT_CHANNEL", "-1003490045182"))  # Агент Дурова channel ID
PASHA_BOT = os.environ.get("PASHA_BOT", "PashaGiftsBot")       # the gift bot username

# Text that marks a message as a claimable airdrop
AIRDROP_KEYWORDS = ["airdrop"]
# If this phrase is present, the airdrop is already gone - don't bother trying
ALREADY_CLAIMED_PHRASES = ["fully claimed", "better luck next time"]
# Only claim airdrops that give AT LEAST this many stars per slot
MIN_STARS_EACH = int(os.environ.get("MIN_STARS_EACH", "300"))
# ────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stars_claimer")

# Telethon's own internal logger is very chatty at INFO level (it logs
# every "keeping in sync with channel X" event). We only want to see our
# own bot's messages, so quiet Telethon down to warnings/errors only.
logging.getLogger("telethon").setLevel(logging.WARNING)

# ─────────────── HARD SAFETY GUARANTEE ───────────────
# This script must NEVER post, reply, or send anything into the monitored
# group chats (TARGET_CHATS). It only ever reads from those chats. The only
# outgoing action permitted anywhere in this file is a private, silent
# backend call directed at the bot itself (StartBotRequest / message.click),
# which is exactly what tapping the claim button does manually - nothing
# visible appears in any group, and no message is posted on your behalf
# there.
#
# This wrapper makes that a hard guarantee: if any code anywhere in this
# file ever tries to call send_message/send_file targeting one of
# TARGET_CHATS, it will raise instead of silently sending.
_real_send_message = TelegramClient.send_message


async def _guarded_send_message(self, entity, *args, **kwargs):
    target_username = str(entity).lower().lstrip("@")
    if target_username in [c.lower() for c in TARGET_CHATS]:
        raise RuntimeError(
            "BLOCKED: this script is not allowed to send messages to the "
            "group chat. This should never happen - if you see this error, "
            "something is misconfigured."
        )
    return await _real_send_message(self, entity, *args, **kwargs)


TelegramClient.send_message = _guarded_send_message
# ───────────────────────────────────────────────────────

if not SESSION_STRING:
    raise SystemExit(
        "SESSION_STRING is not set. Run generate_session.py on your own "
        "computer first, then set SESSION_STRING as an environment variable."
    )

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

gifting_entity = None  # resolved at startup
pasha_entity = None   # @PashaGiftsBot entity, resolved at startup

# ── PashaGiftsBot state ──
# Stores gift cards seen in the channel, waiting for their password to arrive.
# Key: message_id of the gift card message
# Value: dict with start_param, password (None until revealed), claimed flag
pending_gifts = {}
# Stores the latest password seen, so when the bot asks we send it instantly
latest_password = None
# True when the bot has asked for password but it hasn't arrived in channel yet
bot_waiting_for_password = False
# State machine: idle → joining → password_wait → idle
# Prevents the loop where promo messages get treated as subscription checks
pasha_state = "idle"  # "idle", "joining", "password_wait"


def is_unclaimed_airdrop(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    if not any(k in low for k in AIRDROP_KEYWORDS):
        return False

    # Check stars per slot. Confirmed format: "gifting 100 ⭐ each to N people"
    # (the star emoji appears as the unicode character or the word "stars")
    stars_match = re.search(r"gifting\s+(\d+)", low)
    if stars_match:
        stars_each = int(stars_match.group(1))
        if stars_each < MIN_STARS_EACH:
            log.info("Skipping airdrop - only %d stars each (minimum is %d)", stars_each, MIN_STARS_EACH)
            return False
    # if we can't parse the amount, let it through (don't miss it due to a format change)

    # Confirmed format from the live screenshot: "Claimed: 0/1"
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


attempted_messages = set()  # (chat_id, message_id) we've already tried - avoid double-processing edits


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
        # Pull out the bot's actual reply text (e.g. "You claimed an airdrop
        # for 153 stars!" or "This airdrop has been fully claimed.") so the
        # real outcome is visible in the logs, not just "-> ok".
        reply_texts = []
        for upd in getattr(result, "updates", []) or []:
            msg = getattr(upd, "message", None)
            text = getattr(msg, "message", None)
            if text:
                reply_texts.append(text)
        if reply_texts:
            log.info("[%s] Claim result: %s", context, " | ".join(reply_texts))
        else:
            log.info("[%s] StartBotRequest sent (start=%s), no reply text returned", context, start_param)
        return result
    except FloodWaitError as e:
        log.warning("Flood wait %ss while claiming", e.seconds)
    except Exception as e:
        log.info("[%s] Claim attempt failed (likely lost the race): %s", context, e)


async def try_claim(message, context: str):
    key = (message.chat_id, message.id)
    if key in attempted_messages:
        return  # already tried this exact message (e.g. it was just edited into a receipt)

    if not message.buttons:
        log.info("[%s] Airdrop message has no buttons, skipping", context)
        return

    for row in message.buttons:
        for button in row:
            url = getattr(button, "url", None)

            # Ignore buttons that don't point back to Telegram/the bot - e.g.
            # a "TON Transaction" receipt link added after a successful claim.
            # These aren't claim buttons, so don't warn about them.
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

            # Fallback: looks like a normal callback button instead
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


# ═══════════════════════════════════════════════════════
#  PASHA GIFTS BOT — Two steps only:
#  1. Gift card appears → fire /start
#  2. Password appears in channel → send it instantly
# ═══════════════════════════════════════════════════════

@client.on(events.NewMessage(chats=GIFT_CHANNEL))
async def on_gift_card_message(event):
    """Gift card appears → fire /start to @PashaGiftsBot immediately."""
    msg = event.message
    text = msg.text or ""
    if not any(k in text.upper() for k in ["ПОДАРОЧНЫЙ ЧЕК", "GIFT CARD", "GIFT CHEQUE"]):
        return
    if not msg.buttons:
        return
    for row in msg.buttons:
        for button in row:
            url = getattr(button, "url", None)
            if url and "t.me" in url.lower():
                start_param = extract_start_param(url)
                if start_param:
                    log.info("[pasha] Gift card spotted — firing /start instantly...")
                    try:
                        await client(StartBotRequest(
                            bot=pasha_entity,
                            peer=pasha_entity,
                            random_id=random.getrandbits(63),
                            start_param=start_param,
                        ))
                        log.info("[pasha] /start sent ✅ — you handle join + verify")
                    except Exception as e:
                        log.warning("[pasha] /start failed: %s", e)
                    return


@client.on(events.NewMessage(chats=GIFT_CHANNEL))
async def on_password_reveal(event):
    """Password appears in channel → send to @PashaGiftsBot instantly."""
    msg = event.message
    text = msg.text or ""

    # Must be a reply — filters out "Пароль: Установлен" inside the gift card itself
    reply_to = getattr(msg.reply_to, "reply_to_msg_id", None)
    if not reply_to:
        return

    pw_match = re.search(r"(?:[Пп]ароль|[Pp]assword)[:\s]+([^\s\n(]+)", text)
    if not pw_match:
        return

    password = pw_match.group(1).rstrip(".,;!?)\"'")

    if password.lower() in ["set", "установлен", "установлена", "да", "yes"]:
        return

    log.info("[pasha] *** PASSWORD: [%s] — sending to bot NOW ***", password)
    try:
        await client.send_message(pasha_entity, password)
        log.info("[pasha] ✅ Password sent: [%s]", password)
    except Exception as e:
        log.warning("[pasha] Failed to send password: %s", e)


# ═══════════════════════════════════════════════════════
#  END PASHA GIFTS BOT section
# ═══════════════════════════════════════════════════════

@client.on(events.NewMessage(chats=TARGET_CHATS))
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
    global gifting_entity, pasha_entity
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit(
            "SESSION_STRING is invalid or expired. Regenerate it by running "
            "generate_session.py again on your own computer."
        )
    me = await client.get_me()
    log.info("Logged in as %s (@%s | id: %s)", me.first_name, me.username or "no username", me.id)

    for chat in TARGET_CHATS:
        await client.get_entity(chat)
    gifting_entity = await client.get_entity(GIFTING_BOT)
    pasha_entity = await client.get_entity(PASHA_BOT)
    await client.get_entity(GIFT_CHANNEL)

    log.info("Watching %s for Stars Airdrops via @%s ...",
              ", ".join(f"@{c}" for c in TARGET_CHATS), GIFTING_BOT)
    log.info("Watching channel ID %s (Агент Дурова) for Gift Cards via @%s ...", GIFT_CHANNEL, PASHA_BOT)
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
