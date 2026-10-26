"""Log in WITHOUT a code: scan a QR with the Telegram app on the spare account's phone.

    pip install "qrcode[pil]"
    python qr_login.py

Then phone: Telegram > Settings > Devices > Link Desktop Device > scan the QR.
This saves checker_session.session; after that just run: python bot.py
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient, errors

import qrcode

load_dotenv()
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION = os.getenv("SESSION_PATH", "checker_session")


def show(url: str):
    img = qrcode.make(url)
    img.save("qr.png")
    if sys.platform == "win32":
        os.startfile("qr.png")  # opens the QR image on screen
    else:
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.print_ascii(invert=True)


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print("Already logged in as", me.first_name, "- you can run: python bot.py")
        await client.disconnect()
        return
    while True:
        qr = await client.qr_login()
        show(qr.url)
        print("\nOn the PHONE with the spare account:")
        print("Telegram > Settings > Devices > Link Desktop Device > scan the QR on your screen.")
        print("Waiting up to 60 seconds...")
        try:
            await qr.wait(60)
            break
        except asyncio.TimeoutError:
            print("QR expired, making a new one...")
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=input("2FA password: "))
            break
    me = await client.get_me()
    print(f"\nLogged in as {me.first_name} ({me.id}). Now run: python bot.py")
    if os.path.exists("qr.png"):
        os.remove("qr.png")
    await client.disconnect()


asyncio.run(main())
