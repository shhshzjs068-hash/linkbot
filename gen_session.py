"""Run this ONCE on your own PC to create the SESSION_STRING for Railway.

    pip install telethon
    python gen_session.py

Use a spare Telegram account. Treat the printed string like a password:
anyone who has it controls that account. Never commit it to GitHub.
"""
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(os.getenv("API_ID") or input("API_ID: "))
    api_hash = os.getenv("API_HASH") or input("API_HASH: ")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        # prompts for phone, login code (and 2FA password if enabled)
        print("\nSESSION_STRING (put it in Railway Variables):\n")
        print(client.session.save())
        print("\nDo NOT run the bot locally with this same string while it runs on Railway.")


asyncio.run(main())
