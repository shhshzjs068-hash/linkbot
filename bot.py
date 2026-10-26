import asyncio
import csv
import io
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, ChannelForbidden, Chat, ChatForbidden, User

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PHONE = os.getenv("PHONE")  # only for first login when running locally without SESSION_STRING
SESSION_STRING = os.getenv("SESSION_STRING")  # generate with gen_session.py (required on Railway)
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DELAY = float(os.getenv("CHECK_DELAY", "0.8"))  # base seconds between live checks
MAX_FLOOD = int(os.getenv("MAX_FLOOD_WAIT", "1200"))  # longest Telegram rate-limit wait we will sit out (s)
CACHE_TTL = float(os.getenv("CACHE_TTL_HOURS", "6")) * 3600
# On Railway, attach a Volume: RAILWAY_VOLUME_MOUNT_PATH is set automatically and the DB goes there.
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "linkbot.db"))
SESSION = os.getenv("SESSION_PATH", os.path.join(DATA_DIR, "checker_session"))

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(link_preview_is_disabled=True))
dp = Dispatcher()
client = TelegramClient(StringSession(SESSION_STRING) if SESSION_STRING else SESSION, API_ID, API_HASH)
check_lock = asyncio.Lock()  # one live Telegram check at a time (flood protection)
running: dict[int, asyncio.Event] = {}  # user_id -> cancel flag
last_unknown: dict[int, list[str]] = {}  # user_id -> links that couldn't be checked (for /retry)
delay_now = DELAY  # adaptive: grows after rate limits, shrinks back when things go well

# ----------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS settings(
    user_id INTEGER PRIMARY KEY, kind TEXT DEFAULT 'all', min_members INTEGER DEFAULT 0,
    max_inactive INTEGER DEFAULT 0, no_join_req INTEGER DEFAULT 0, detail INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cache(id TEXT PRIMARY KEY, data TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS history(
    user_id INTEGER, ts REAL, total INTEGER, working INTEGER, violated INTEGER,
    dead INTEGER, filtered INTEGER);
"""
)
db.execute("DELETE FROM cache WHERE ts < ?", (time.time() - 7 * 86400,))
db.commit()

SETTING_FIELDS = {"kind", "min_members", "max_inactive", "no_join_req", "detail"}


def get_settings(uid: int) -> dict:
    db.execute("INSERT OR IGNORE INTO settings(user_id) VALUES (?)", (uid,))
    db.commit()
    return dict(db.execute("SELECT * FROM settings WHERE user_id=?", (uid,)).fetchone())


def set_setting(uid: int, field: str, value):
    assert field in SETTING_FIELDS
    get_settings(uid)
    db.execute(f"UPDATE settings SET {field}=? WHERE user_id=?", (value, uid))
    db.commit()


def cache_get(cid: str):
    row = db.execute("SELECT data, ts FROM cache WHERE id=?", (cid,)).fetchone()
    if row and time.time() - row["ts"] < CACHE_TTL:
        return json.loads(row["data"])
    return None


def cache_put(cid: str, res: dict):
    db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (cid, json.dumps(res), time.time()))
    db.commit()


# ------------------------------------------------------------------ parsing
LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/([^\s<>\"')\]]+)", re.I)
TG_RE = re.compile(r"tg://(?:join\?invite=([\w-]+)|resolve\?domain=(\w+))", re.I)
MENTION_RE = re.compile(r"(?<![\w./@])@([A-Za-z]\w{3,31})\b")
USERNAME_RE = re.compile(r"[A-Za-z]\w{3,31}")
INVITE_RE = re.compile(r"[\w-]{8,}")
RESERVED = {"c", "addstickers", "addemoji", "proxy", "socks", "share", "setlanguage",
            "login", "boost", "iv", "bg", "addtheme", "confirmphone", "joinchat"}


def _item(kind: str, key: str):
    if kind == "invite":
        return {"id": f"invite:{key}", "kind": kind, "key": key, "link": f"https://t.me/+{key}"}
    return {"id": f"public:{key.lower()}", "kind": kind, "key": key, "link": f"https://t.me/{key}"}


def parse_links(text: str) -> list[dict]:
    found = []  # (position, item)
    for m in LINK_RE.finditer(text):
        path = m.group(1).rstrip(".,;:!?")
        parts = path.split("?")[0].split("/")
        first = parts[0]
        if first.startswith("+"):
            key = first[1:]
            if INVITE_RE.fullmatch(key):
                found.append((m.start(), _item("invite", key)))
        elif first.lower() == "joinchat" and len(parts) > 1:
            if INVITE_RE.fullmatch(parts[1]):
                found.append((m.start(), _item("invite", parts[1])))
        else:
            if first.lower() == "s" and len(parts) > 1:  # t.me/s/channel (web preview)
                first = parts[1]
            if first.lower() in RESERVED or not USERNAME_RE.fullmatch(first):
                continue
            found.append((m.start(), _item("public", first)))
    for m in TG_RE.finditer(text):
        if m.group(1) and INVITE_RE.fullmatch(m.group(1)):
            found.append((m.start(), _item("invite", m.group(1))))
        elif m.group(2) and USERNAME_RE.fullmatch(m.group(2)):
            found.append((m.start(), _item("public", m.group(2))))
    for m in MENTION_RE.finditer(text):
        found.append((m.start(), _item("public", m.group(1))))

    seen, out = set(), []
    for _, it in sorted(found, key=lambda x: x[0]):
        if it["id"] not in seen:
            seen.add(it["id"])
            out.append(it)
    return out


def extract_text(msg: Message) -> str:
    """Text + hidden hyperlinks + inline-button URLs."""
    text = msg.text or msg.caption or ""
    extra = [e.url for e in (msg.entities or msg.caption_entities or [])
             if e.type == "text_link" and e.url]
    if msg.reply_markup and getattr(msg.reply_markup, "inline_keyboard", None):
        for row in msg.reply_markup.inline_keyboard:
            extra += [b.url for b in row if b.url]
    return text + "\n" + "\n".join(extra)


# ----------------------------------------------------------------- checking
def fin(res: dict, status: str, reason: str = "") -> dict:
    res["status"], res["reason"] = status, reason
    return res


async def entity_result(res: dict, ent, want_activity: bool) -> dict:
    if isinstance(ent, User):
        return fin(res, "dead", "user/bot, not a channel or group")
    if isinstance(ent, (ChannelForbidden, ChatForbidden)):
        res["title"] = getattr(ent, "title", "") or ""
        return fin(res, "violated", "banned/forbidden")
    res["title"] = getattr(ent, "title", "") or ""

    if isinstance(ent, Chat):
        res["type"] = "group"
        res["members"] = getattr(ent, "participants_count", None)
        if getattr(ent, "deactivated", False):
            return fin(res, "dead", "deactivated")
        return fin(res, "working")
    if not isinstance(ent, Channel):
        return fin(res, "unknown", f"unsupported: {type(ent).__name__}")

    res["type"] = "channel" if ent.broadcast else ("forum" if getattr(ent, "forum", False) else "group")
    res["verified"] = bool(getattr(ent, "verified", False))
    res["join_request"] = bool(getattr(ent, "join_request", False))
    res["members"] = getattr(ent, "participants_count", None)

    # --- violation flags set by Telegram itself
    if getattr(ent, "restricted", False):
        reasons = sorted({r.reason for r in (getattr(ent, "restriction_reason", None) or [])})
        return fin(res, "violated", "restricted" + (f" ({', '.join(reasons)})" if reasons else ""))
    if getattr(ent, "scam", False):
        return fin(res, "violated", "marked SCAM by Telegram")
    if getattr(ent, "fake", False):
        return fin(res, "violated", "marked FAKE by Telegram")

    if res["members"] is None:  # public lookups often omit the member count
        try:
            full = await client(GetFullChannelRequest(ent))
            res["members"] = full.full_chat.participants_count
        except errors.FloodWaitError:
            raise
        except Exception:  # noqa: BLE001
            pass
    if want_activity:
        try:
            msgs = await client.get_messages(ent, limit=1)
            if msgs:
                res["last_post"] = msgs[0].date.isoformat()
        except errors.FloodWaitError:
            raise
        except Exception:  # noqa: BLE001
            pass
    return fin(res, "working")


def invite_result(res: dict, r) -> dict:
    res["title"] = getattr(r, "title", "") or ""
    res["members"] = getattr(r, "participants_count", None)
    res["type"] = "channel" if getattr(r, "broadcast", False) else "group"
    res["join_request"] = bool(getattr(r, "request_needed", False))
    res["verified"] = bool(getattr(r, "verified", False))
    if getattr(r, "scam", False):
        return fin(res, "violated", "marked SCAM by Telegram")
    if getattr(r, "fake", False):
        return fin(res, "violated", "marked FAKE by Telegram")
    return fin(res, "working")


async def flood_sleep(seconds: int, ctl: dict | None) -> bool:
    """Sit out a Telegram rate limit (cancellable). Slows future checks down. False if cancelled."""
    global delay_now
    delay_now = min(delay_now * 1.5 + 0.5, 6.0)
    if ctl and ctl.get("notify"):
        await ctl["notify"](seconds)
    end = time.time() + seconds
    while time.time() < end:
        if ctl and ctl["cancel"].is_set():
            return False
        await asyncio.sleep(max(0.0, min(5.0, end - time.time())))
    return True


async def check_link(l: dict, want_activity: bool, ctl: dict | None = None) -> dict:
    res = {"id": l["id"], "kind": l["kind"], "key": l["key"], "link": l["link"],
           "status": "unknown", "reason": "", "title": "", "members": None, "type": "",
           "join_request": False, "verified": False, "last_post": None,
           "has_activity": want_activity}
    for _ in range(6):
        try:
            if l["kind"] == "invite":
                r = await client(CheckChatInviteRequest(l["key"]))
                if getattr(r, "chat", None) is not None:  # already a member / peek
                    return await entity_result(res, r.chat, want_activity)
                return invite_result(res, r)
            ent = await client.get_entity(l["key"])
            return await entity_result(res, ent, want_activity)
        except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
            return fin(res, "dead", "expired/invalid invite")
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
            return fin(res, "dead", "username not found")
        except (errors.ChannelPrivateError, errors.ChannelBannedError):
            return fin(res, "violated", "banned/unavailable")
        except errors.FloodWaitError as e:
            if e.seconds > MAX_FLOOD:
                return fin(res, "unknown", f"rate limit {e.seconds}s (too long, try /retry later)")
            if not await flood_sleep(e.seconds + 1, ctl):
                return fin(res, "unknown", "cancelled")
        except Exception as e:  # noqa: BLE001
            logging.exception("check failed for %s", l["link"])
            return fin(res, "unknown", type(e).__name__)
    return fin(res, "unknown", "retry limit")


async def get_result(l: dict, cfg: dict, ctl: dict | None = None) -> dict:
    global delay_now
    need_activity = cfg["max_inactive"] > 0 and l["kind"] == "public"
    cached = cache_get(l["id"])
    if cached and (not need_activity or cached.get("has_activity") or cached["status"] != "working"):
        cached["cached"] = True
        return cached
    async with check_lock:
        res = await check_link(l, need_activity, ctl)
        if res["status"] != "unknown":
            delay_now = max(DELAY, delay_now * 0.95)
        await asyncio.sleep(delay_now)
    if res["status"] != "unknown":
        cache_put(l["id"], res)
    res["cached"] = False
    return res


# ---------------------------------------------------------------- filtering
def days_ago(iso: str) -> int:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).days


def classify(res: dict, cfg: dict) -> tuple[str, str]:
    """Returns (bucket, reason). Buckets: working/violated/filtered/dead/unknown."""
    if res["status"] != "working":
        return res["status"], res["reason"]
    if cfg["kind"] == "channel" and res["type"] != "channel":
        return "filtered", "not a channel"
    if cfg["kind"] == "group" and res["type"] == "channel":
        return "filtered", "not a group"
    m = res["members"]
    if cfg["min_members"] and m is not None and m < cfg["min_members"]:
        return "filtered", f"{m} members (< {cfg['min_members']})"
    if cfg["max_inactive"] and res["last_post"]:
        d = days_ago(res["last_post"])
        if d > cfg["max_inactive"]:
            return "filtered", f"inactive for {d} days"
    if cfg["no_join_req"] and res["join_request"]:
        return "filtered", "join request required"
    return "working", ""


def fmt_members(n) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def line(res: dict, detail: bool) -> str:
    if not detail:
        return res["link"]
    bits = [res["type"] or "?", f"{fmt_members(res['members'])} members"]
    if res["last_post"]:
        bits.append(f"last post {days_ago(res['last_post'])}d ago")
    if res["verified"]:
        bits.append("verified")
    if res["join_request"]:
        bits.append("join request")
    return f"{res['link']} - {res['title']} [{', '.join(bits)}]"


# -------------------------------------------------------------------- utils
def allowed_id(uid: int | None) -> bool:
    return not ADMIN_IDS or (uid in ADMIN_IDS)


async def send_chunks(msg: Message, lines: list[str], header: str = ""):
    buf = header
    for ln in lines:
        if len(buf) + len(ln) + 1 > 3900:
            await msg.answer(buf)
            buf = ""
        buf += ln + "\n"
    if buf.strip():
        await msg.answer(buf)


def doc(name: str, text: str, bom: bool = False) -> BufferedInputFile:
    return BufferedInputFile(text.encode("utf-8-sig" if bom else "utf-8"), name)


def cancel_kb():
    b = InlineKeyboardBuilder()
    b.button(text="Cancel", callback_data="cancel")
    return b.as_markup()


async def safe_edit(m: Message, text: str, kb=None):
    try:
        await m.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ the job
async def process(msg: Message, text: str):
    uid = msg.from_user.id
    if uid in running:
        return await msg.answer("You already have a job running. Wait for it or press Cancel.")
    links = parse_links(text)
    if not links:
        return await msg.answer("No Telegram links found.")

    cfg = get_settings(uid)
    cancel = asyncio.Event()
    running[uid] = cancel
    status = await msg.answer(f"Checking 0/{len(links)}...", reply_markup=cancel_kb())
    results, last_edit = [], 0.0
    state = {"i": 0}

    async def notify(secs: int):
        await safe_edit(status, f"Telegram rate limit hit. Waiting {secs}s, then continuing "
                                f"({state['i']}/{len(links)} done)...", cancel_kb())

    ctl = {"cancel": cancel, "notify": notify}
    try:
        for i, l in enumerate(links, 1):
            if cancel.is_set():
                break
            state["i"] = i - 1
            results.append(await get_result(l, cfg, ctl))
            if time.time() - last_edit > 2.5:
                last_edit = time.time()
                await safe_edit(status, f"Checking {i}/{len(links)}...", cancel_kb())
    finally:
        running.pop(uid, None)

    buckets = {k: [] for k in ("working", "violated", "filtered", "dead", "unknown")}
    for res in results:
        b, why = classify(res, cfg)
        res["bucket"], res["why"] = b, why
        buckets[b].append(res)
    buckets["working"].sort(key=lambda r: r["members"] or 0, reverse=True)
    last_unknown[uid] = [r["link"] for r in buckets["unknown"]] + [l["link"] for l in links[len(results):]]
    why_unknown = {}
    for r in buckets["unknown"]:
        k = re.sub(r"\d+", "N", r["reason"] or "unknown")
        why_unknown[k] = why_unknown.get(k, 0) + 1
    unk_note = ""
    if why_unknown:
        unk_note = ("\nUnchecked because: " + ", ".join(f"{k} x{v}" for k, v in why_unknown.items())
                    + "\nSend /retry to re-check them.")

    cached_n = sum(1 for r in results if r.get("cached"))
    head = "Cancelled early." if cancel.is_set() else "Done."
    await safe_edit(
        status,
        f"{head} {len(results)}/{len(links)} checked ({cached_n} from cache)\n\n"
        f"Working: {len(buckets['working'])}\n"
        f"Violated/restricted (kept aside): {len(buckets['violated'])}\n"
        f"Filtered by your settings (kept aside): {len(buckets['filtered'])}\n"
        f"Dead/expired: {len(buckets['dead'])}\n"
        f"Couldn't check: {len(buckets['unknown'])}" + unk_note,
    )

    if buckets["working"]:
        lines = [line(r, bool(cfg["detail"])) for r in buckets["working"]]
        await send_chunks(msg, lines, "Working links:\n\n")
        if len(lines) > 25:
            await msg.answer_document(doc("working.txt", "\n".join(r["link"] for r in buckets["working"])))
    else:
        await msg.answer("No working links.")

    for name, key in (("violated.txt", "violated"), ("filtered_out.txt", "filtered"),
                      ("couldnt_check.txt", "unknown")):
        if buckets[key]:
            body = "\n".join(f"{r['link']} - {r['why'] or r['reason']}" for r in buckets[key])
            await msg.answer_document(doc(name, body), caption=f"{key.capitalize()}: {len(buckets[key])}")

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["link", "bucket", "reason", "title", "type", "members", "last_post", "verified", "join_request"])
    for r in results:
        w.writerow([r["link"], r["bucket"], r["why"] or r["reason"], r["title"], r["type"],
                    r["members"], r["last_post"] or "", r["verified"], r["join_request"]])
    await msg.answer_document(doc("results.csv", out.getvalue(), bom=True), caption="Full report")

    db.execute("INSERT INTO history VALUES (?,?,?,?,?,?,?)",
               (uid, time.time(), len(results), len(buckets["working"]), len(buckets["violated"]),
                len(buckets["dead"]), len(buckets["filtered"])))
    db.commit()


# ----------------------------------------------------------------- settings
KINDS = ["all", "channel", "group"]
MIN_MEMBERS = [0, 100, 500, 1000, 5000, 10000]
INACTIVE = [0, 7, 30, 90]


def cycle(vals, cur):
    return vals[(vals.index(cur) + 1) % len(vals)] if cur in vals else vals[0]


def settings_view(cfg: dict):
    b = InlineKeyboardBuilder()
    b.button(text=f"Type: {cfg['kind']}", callback_data="set:kind")
    b.button(text=f"Min members: {cfg['min_members'] or 'off'}", callback_data="set:min")
    b.button(text=f"Max inactivity: {str(cfg['max_inactive']) + ' days' if cfg['max_inactive'] else 'off'}",
             callback_data="set:act")
    b.button(text=f"Join-request links: {'hide' if cfg['no_join_req'] else 'show'}", callback_data="set:jr")
    b.button(text=f"Detailed lines: {'on' if cfg['detail'] else 'off'}", callback_data="set:det")
    b.adjust(1)
    text = ("Filters (applied to working links; anything filtered out is kept aside in a file).\n"
            "Max inactivity needs an extra call per public channel, so big batches run slower.")
    return text, b.as_markup()


@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    if allowed_id(msg.from_user.id):
        text, kb = settings_view(get_settings(msg.from_user.id))
        await msg.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("set:"))
async def cb_set(cb: CallbackQuery):
    uid = cb.from_user.id
    if not allowed_id(uid):
        return await cb.answer()
    cfg = get_settings(uid)
    what = cb.data.split(":")[1]
    if what == "kind":
        set_setting(uid, "kind", cycle(KINDS, cfg["kind"]))
    elif what == "min":
        set_setting(uid, "min_members", cycle(MIN_MEMBERS, cfg["min_members"]))
    elif what == "act":
        set_setting(uid, "max_inactive", cycle(INACTIVE, cfg["max_inactive"]))
    elif what == "jr":
        set_setting(uid, "no_join_req", 0 if cfg["no_join_req"] else 1)
    elif what == "det":
        set_setting(uid, "detail", 0 if cfg["detail"] else 1)
    text, kb = settings_view(get_settings(uid))
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery):
    ev = running.get(cb.from_user.id)
    if ev:
        ev.set()
    await cb.answer("Cancelling...")


# ----------------------------------------------------------------- handlers
@dp.message(Command("start", "help"))
async def cmd_start(msg: Message):
    if not allowed_id(msg.from_user.id):
        return
    await msg.answer(
        "Send me Telegram links in any form: pasted text, a .txt/.csv file, forwarded posts, "
        "@usernames, hidden hyperlinks, even inline buttons. I'll reply with only the working ones.\n\n"
        "Violated/restricted/scam channels, dead links, and anything your filters remove are kept "
        "aside in separate files, plus a full CSV report.\n\n"
        "/settings - filters (type, min members, activity, join-request)\n"
        "/stats - your totals\n"
        "/retry - re-check links that couldn't be checked (rate limits)\n"
        "/cancel - stop the running job\n"
        "/clearcache - forget cached results")


@dp.message(Command("retry"))
async def cmd_retry(msg: Message):
    if not allowed_id(msg.from_user.id):
        return
    links = last_unknown.get(msg.from_user.id)
    if not links:
        return await msg.answer("Nothing to retry.")
    await process(msg, "\n".join(links))


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message):
    ev = running.get(msg.from_user.id)
    if ev:
        ev.set()
        await msg.answer("Cancelling...")
    else:
        await msg.answer("Nothing running.")


@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not allowed_id(msg.from_user.id):
        return
    r = db.execute("SELECT COUNT(*) n, COALESCE(SUM(total),0) t, COALESCE(SUM(working),0) w, "
                   "COALESCE(SUM(violated),0) v, COALESCE(SUM(dead),0) d FROM history WHERE user_id=?",
                   (msg.from_user.id,)).fetchone()
    await msg.answer(f"Jobs: {r['n']}\nLinks checked: {r['t']}\nWorking: {r['w']}\n"
                     f"Violated: {r['v']}\nDead: {r['d']}")


@dp.message(Command("clearcache"))
async def cmd_clearcache(msg: Message):
    if not allowed_id(msg.from_user.id):
        return
    db.execute("DELETE FROM cache")
    db.commit()
    await msg.answer("Cache cleared.")


@dp.message(F.document)
async def on_file(msg: Message):
    if not allowed_id(msg.from_user.id):
        return
    if msg.document.file_size and msg.document.file_size > 2_000_000:
        return await msg.answer("File too big (max 2 MB).")
    buf = io.BytesIO()
    await bot.download(msg.document, destination=buf)
    await process(msg, buf.getvalue().decode("utf-8", errors="ignore") + "\n" + extract_text(msg))


@dp.message(F.text | F.caption)
async def on_text(msg: Message):
    if not allowed_id(msg.from_user.id):
        return
    if msg.text and msg.text.startswith("/"):
        return
    await process(msg, extract_text(msg))


async def main():
    if SESSION_STRING:
        # Non-interactive (Railway): never prompt for a code, fail loudly instead.
        await client.connect()
        if not await client.is_user_authorized():
            raise SystemExit("SESSION_STRING is invalid or expired. Regenerate it with gen_session.py")
    else:
        await (client.start(phone=PHONE) if PHONE else client.start())
    me = await client.get_me()
    logging.info("Checker account: %s (%s)", me.first_name, me.id)
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS is empty: ANYONE can use this bot and burn your checker account's limits")

    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="settings", description="Filters: type, members, activity"),
        BotCommand(command="stats", description="Your totals"),
        BotCommand(command="retry", description="Re-check unchecked links"),
        BotCommand(command="cancel", description="Stop the running job"),
        BotCommand(command="clearcache", description="Forget cached results"),
        BotCommand(command="help", description="How to use"),
    ])
    try:
        await dp.start_polling(bot)
    finally:
        await client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
