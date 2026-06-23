"""
Run this ONCE on your own computer (NOT on Railway) to log into your
Telegram account interactively. It will print a "session string" -
copy that and save it somewhere safe, you'll paste it into Railway
as an environment variable.

Usage:
    pip install telethon
    python generate_session.py
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = 1234567                  # same value you'll use on Railway
API_HASH = "your_api_hash_here"   # same value you'll use on Railway

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\nLogged in successfully. Your session string is below.")
    print("Keep it secret - anyone with this string can access your account.\n")
    print(client.session.save())
    print("\nCopy the line above into Railway as SESSION_STRING.")
