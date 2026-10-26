"""Make a SESSION_STRING for Railway by scanning a QR (no login code needed).

    pip install telethon "qrcode[pil]"
    python gen_session_qr.py

Phone (spare account): Telegram > Settings > Devices > Link Desktop Device > scan the QR.
The string is printed once. Put it ONLY in Railway Variables. Anyone with it controls the account.
It creates a separate login, so your PC session keeps working, but don't run the bot in
two places at once with the same BOT_TOKEN.
"""
import asyncio
import os
import sys

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

import qrcode


def show(url: str):
    img = qrcode.make(url)
    img.save("qr.png")
    if sys.platform == "win32":
        os.startfile("qr.png")
    else:
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.print_ascii(invert=True)


async def main():
    api_id = int(os.getenv("API_ID") or input("API_ID: "))
    api_hash = os.getenv("API_HASH") or input("API_HASH: ")
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    while True:
        qr = await client.qr_login()
        show(qr.url)
        print("\nScan with the spare account: Settings > Devices > Link Desktop Device. Waiting 60s...")
        try:
            await qr.wait(60)
            break
        except asyncio.TimeoutError:
            print("QR expired, making a new one...")
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=input("2FA password: "))
            break
    me = await client.get_me()
    print(f"\nLogged in as {me.first_name} ({me.id}).\n\nSESSION_STRING (copy it, put it in Railway Variables):\n")
    print(client.session.save())
    if os.path.exists("qr.png"):
        os.remove("qr.png")
    await client.disconnect()


asyncio.run(main())
