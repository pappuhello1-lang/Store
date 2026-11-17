import asyncio
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from io import BytesIO

import aiohttp
import qrcode
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telethon import TelegramClient, events

# ================= CONFIGURATION =================
BOT_TOKEN = "9243834431:AAGvS2F5EQoTSzy6jbU1DN5-n__StYKWeHY"           # @BotFather se mila token
ADMIN_ID =  1069319252                          # Aapka Telegram Numeric ID

# Userbot Credentials (my.telegram.org)
API_ID = 28197998  # Replace with your regenerated/current API ID
API_HASH = "190380189baa4e57e8d24fe06b51a6e9"         # Apna full API_HASH yahan paste karein

# Provider Bot & Channels
PROVIDER_BOT = "@HITXVIPXBOT"
FORCE_JOIN_CHANNEL_ID = -1004445184980
FORCE_JOIN_LINK = "https://t.me/+H8hfyJRq-0EzOTdl"  # Apne channel ka public/invite link daalein
LOG_GROUP_ID = -5036677063

# Paytm Business Details
PAYTM_MID = "IWTFev59542546537042"
PAYTM_UPI_ID = "paytm.s3lvd5i@pty"  # Apni Paytm Business UPI ID daalein
MERCHANT_NAME = "Dream Events X Sanjeet Flowers"

# Anti-Abuse Settings for Free Key
MAX_FREE_KEYS_PER_DAY = 20  # Maximum free keys per day

# Paid Plans (Price aur @HITXVIPXBOT ke exact commands)
PLANS = {
    "1h": {"label": "1 Hour Key - ₹15", "price": 15, "cmd": "/gen 1h"},
    "2h": {"label": "2 Hours Key - ₹25", "price": 25, "cmd": "/gen 2h"},
    "5h": {"label": "5 Hours Key - ₹39", "price": 39, "cmd": "/gen 5h"},
    "12h": {"label": "12 Hours Key - ₹69", "price": 69, "cmd": "/gen 12h"},
    "24h": {"label": "24 Hours Key - ₹99", "price": 99, "cmd": "/gen 24h"},
    "7d": {"label": "7 Days Key - ₹299", "price": 299, "cmd": "/gen 7d"},
}
# =================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
userbot = TelegramClient("reseller_session", API_ID, API_HASH)

# Concurrency Locks
generation_lock = asyncio.Lock()
active_free_claims = set()  # Spam click rokne ke liye

# --- DATABASE SETUP (Persistent SQLite) ---
conn = sqlite3.connect("bot_records.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS free_claims (
    user_id INTEGER PRIMARY KEY,
    last_claimed_at INTEGER NOT NULL
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS daily_stats (
    date_str TEXT PRIMARY KEY,
    free_count INTEGER DEFAULT 0
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    user_id INTEGER,
    plan TEXT,
    amount REAL,
    status TEXT,
    delivered_key TEXT,
    created_at INTEGER
)
""")
conn.commit()


# --- HELPER FUNCTIONS ---
def get_today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_today_free_count() -> int:
    cursor.execute("SELECT free_count FROM daily_stats WHERE date_str = ?", (get_today_str(),))
    row = cursor.fetchone()
    return row[0] if row else 0


def increment_today_free_count():
    today = get_today_str()
    cursor.execute(
        "INSERT INTO daily_stats (date_str, free_count) VALUES (?, 1) "
        "ON CONFLICT(date_str) DO UPDATE SET free_count = free_count + 1",
        (today,),
    )
    conn.commit()


async def is_user_joined(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL_ID, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


def extract_key(text: str) -> str:
    """@HITXVIPXBOT ke reply se key extract karega"""
    # 1. Backticks (`KEY`) check karein
    m = re.search(r"`([A-Za-z0-9\-_]{5,})`", text)
    if m:
        return m.group(1)
    # 2. 'Key: XXXX' ya 'License: XXXX' check karein
    m = re.search(r"(?:Key|License|Token|Code)\s*[:\-]\s*([A-Za-z0-9\-_]{5,})", text, re.IGNORECASE)
    if m:
        return m.group(1)
    # 3. Fallback: pura text return karega
    return text.strip()


async def generate_from_provider(cmd_str: str) -> str | None:
    """Userbot ke through @HITXVIPXBOT ko command bhej kar key layega"""
    async with generation_lock:
        future = asyncio.get_running_loop().create_future()

        @userbot.on(events.NewMessage(from_users=PROVIDER_BOT))
        async def handler(event):
            if not future.done():
                future.set_result(event.raw_text)

        try:
            await userbot.send_message(PROVIDER_BOT, cmd_str)
            reply_text = await asyncio.wait_for(future, timeout=15.0)
            return extract_key(reply_text)
        except asyncio.TimeoutError:
            return None
        finally:
            userbot.remove_event_handler(handler)


async def send_log_to_group(text: str):
    try:
        await bot.send_message(chat_id=LOG_GROUP_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        print(f"Log group error: {e}")


# --- QR & PAYTM POLLING ---
def generate_upi_qr(order_id: str, amount: int) -> BytesIO:
    upi_url = (
        f"upi://pay?pa={PAYTM_UPI_ID}"
        f"&pn={MERCHANT_NAME.replace(' ', '%20')}"
        f"&am={amount:.2f}&tr={order_id}&tn={order_id}&cu=INR"
    )
    img = qrcode.make(upi_url)
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


async def check_paytm_status(order_id: str) -> dict:
    url = "https://securegw.paytm.in/merchant-status/getTxnStatus"
    payload = json.dumps({"MID": PAYTM_MID, "ORDERID": order_id})
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params={"JsonData": payload}, timeout=10) as resp:
            return json.loads(await resp.text())


async def poll_payment_and_deliver(user: types.User, chat_id: int, msg_id: int, order_id: str, plan: str, amount: int):
    for _ in range(36):  # 3 minute (36 x 5s)
        await asyncio.sleep(5)
        try:
            res = await check_paytm_status(order_id)
            if res.get("STATUS") == "TXN_SUCCESS" and float(res.get("TXNAMOUNT", 0)) >= amount:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=msg_id,
                    caption="✅ **Payment Verified!** Key generate ho rahi hai, 2 second rukiye...",
                    parse_mode="Markdown",
                )

                cmd_to_run = PLANS[plan]["cmd"]
                new_key = await generate_from_provider(cmd_to_run)
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)

                username_str = f"@{user.username}" if user.username else user.full_name

                if new_key:
                    cursor.execute(
                        "UPDATE orders SET status = 'SUCCESS', delivered_key = ? WHERE order_id = ?",
                        (new_key, order_id),
                    )
                    conn.commit()

                    await bot.send_message(
                        chat_id,
                        f"🎉 **Payment Successful!**\n"
                        f"🆔 Order ID: `{order_id}`\n"
                        f"📦 Plan: **{PLANS[plan]['label']}**\n\n"
                        f"🔑 **Aapki Key:**\n`{new_key}`",
                        parse_mode="Markdown",
                    )

                    # Log Group mein record bhejein
                    await send_log_to_group(
                        f"💰 **NEW PAID ORDER**\n"
                        f"👤 User: {username_str} (`{user.id}`)\n"
                        f"📦 Plan: **{plan}**\n"
                        f"💵 Paid: **₹{amount}**\n"
                        f"🆔 Order: `{order_id}`\n"
                        f"🔑 Key: `{new_key}`"
                    )
                else:
                    cursor.execute("UPDATE orders SET status = 'PAID_NO_KEY' WHERE order_id = ?", (order_id,))
                    conn.commit()

                    await bot.send_message(
                        chat_id,
                        f"⚠️ Payment verify ho gayi (`{order_id}`), lekin key generation mein delay hai. Admin ko alert bhej diya gaya hai!",
                        parse_mode="Markdown",
                    )
                    await send_log_to_group(
                        f"🚨 **MANUAL KEY REQUIRED**\n"
                        f"👤 User: {username_str} (`{user.id}`)\n"
                        f"💵 Paid: **₹{amount}** | Plan: **{plan}**\n"
                        f"🆔 Order: `{order_id}`"
                    )
                return
        except Exception:
            pass

    cursor.execute("UPDATE orders SET status = 'EXPIRED' WHERE order_id = ?", (order_id,))
    conn.commit()
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=msg_id,
            caption=f"⌛ **ORDER EXPIRED**\nOrder: `{order_id}`\n\nUse /start to create a new order.",
            parse_mode="Markdown",
        )
    except Exception:
        pass


# --- MENU & HANDLERS ---
def main_menu_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🎁  CLAIM FREE KEY  •  DAILY", callback_data="claim_free")]
    ]
    for code, info in PLANS.items():
        buttons.append([InlineKeyboardButton(text=f"💎  {info['label']}  •  INSTANT", callback_data=f"buy:{code}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "╭━━━〔 💎 HITX VIP KEY STORE 〕━━━╮\n"
        "│\n"
        "│  ⚡ FAST • SECURE • AUTO DELIVERY\n"
        "│  🎁 Daily free key available\n"
        "│\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "👇 **SELECT YOUR KEY PLAN**\n"
        "Choose a package below or claim your daily reward:",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "claim_free")
async def process_free_claim(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    # 1. Anti-Spam Click Lock
    if user_id in active_free_claims:
        return await callback.answer("⏳ Aapki request process ho rahi hai, rukiye...", show_alert=True)

    active_free_claims.add(user_id)
    try:
        # 2. Compulsory Channel Join Check
        if not await is_user_joined(user_id):
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📢 Join Official Channel", url=FORCE_JOIN_LINK)],
                    [InlineKeyboardButton(text="✅ Joined! Claim Now", callback_data="claim_free")],
                ]
            )
            await callback.answer()
            return await callback.message.answer(
                "🔒 **MEMBERS-ONLY REWARD**\n\nJoin our official channel first, then tap **Joined! Claim Now** to unlock your free key.",
                reply_markup=kb,
                parse_mode="Markdown",
            )

        # 3. 24-Hour Per-User Lock Check
        now = int(time.time())
        cursor.execute("SELECT last_claimed_at FROM free_claims WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            elapsed = now - row[0]
            if elapsed < 86400:  # 24 Hours = 86400 seconds
                rem = 86400 - elapsed
                hrs = rem // 3600
                mins = (rem % 3600) // 60
                return await callback.answer(
                    f"⏳ Aap aaj ki free key le chuke hain!\nDobara {hrs}h {mins}m baad claim karein.",
                    show_alert=True,
                )

        # 4. Global Daily Limit Check
        if get_today_free_count() >= MAX_FREE_KEYS_PER_DAY:
            return await callback.answer(
                "⚠️ Aaj ka daily free quota full ho gaya hai! Kal try karein ya paid plan lein.",
                show_alert=True,
            )

        await callback.answer("🎁 Preparing your daily reward...")
        status_msg = await callback.message.answer("╭━━〔 ⚙️ GENERATING KEY 〕━━╮\n│  Your free key is being prepared...\n│  Please wait a moment.\n╰━━━━━━━━━━━━━━━━━━━━╯", parse_mode="Markdown")

        # Generate 1hr key via @HITXVIPXBOT
        free_key = await generate_from_provider("/gen 1h")
        await status_msg.delete()

        if free_key:
            # Save timestamp in DB
            cursor.execute(
                "INSERT INTO free_claims (user_id, last_claimed_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_claimed_at = ?",
                (user_id, now, now),
            )
            conn.commit()
            increment_today_free_count()

            await callback.message.answer(
                f"╭━━〔 🎉 REWARD UNLOCKED 〕━━╮\n"
                f"│  **DAILY FREE 1-HOUR KEY**\n"
                f"│\n"
                f"│  🔑 **YOUR KEY**\n"
                f"│  `{free_key}`\n"
                f"│\n"
                f"╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"⏳ Your next free claim is available in **24 hours**.",
                parse_mode="Markdown",
            )

            username_str = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
            await send_log_to_group(
                f"🎁 **FREE 1HR KEY CLAIMED**\n"
                f"👤 User: {username_str} (`{user_id}`)\n"
                f"🔑 Key: `{free_key}`\n"
                f"📊 Today's Free Count: `{get_today_free_count()}/{MAX_FREE_KEYS_PER_DAY}`"
            )
        else:
            await callback.message.answer("⚠️ **TEMPORARILY UNAVAILABLE**\n\nThe key server is busy. Please try again in a minute.")
    finally:
        active_free_claims.discard(user_id)


@dp.callback_query(F.data.startswith("buy:"))
async def process_buy(callback: types.CallbackQuery):
    plan = callback.data.split(":")[1]
    if plan not in PLANS:
        return

    await callback.answer("QR Code generate ho raha hai...")
    amount = PLANS[plan]["price"]
    order_id = f"ORD{int(time.time())}{uuid.uuid4().hex[:4].upper()}"

    cursor.execute(
        "INSERT INTO orders (order_id, user_id, plan, amount, status, created_at) VALUES (?, ?, ?, ?, 'PENDING', ?)",
        (order_id, callback.from_user.id, plan, amount, int(time.time())),
    )
    conn.commit()

    qr_bio = generate_upi_qr(order_id, amount)
    photo = BufferedInputFile(qr_bio.read(), filename="qr.png")

    sent_msg = await callback.message.answer_photo(
        photo=photo,
        caption=(
            f"╭━━━〔 💳 SECURE CHECKOUT 〕━━━╮\n"
            f"│  **AMOUNT: ₹{amount}**\n"
            f"│  📦 Plan: **{PLANS[plan]['label']}**\n"
            f"│  🧾 Order: `{order_id}`\n"
            f"│\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"📲 Scan the QR using Paytm or PhonePe.\n"
            f"⚡ Your key will be delivered automatically after payment is verified."
        ),
        parse_mode="Markdown",
    )

    asyncio.create_task(
        poll_payment_and_deliver(callback.from_user, callback.message.chat.id, sent_msg.message_id, order_id, plan, amount)
    )


# --- ADMIN COMMANDS ---
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    cursor.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM orders WHERE status = 'SUCCESS'")
    total_orders, total_rev = cursor.fetchone()
    await message.reply(
        f"╭━━〔 📊 STORE DASHBOARD 〕━━╮\n"
        f"│  💳 Successful Orders: **{total_orders}**\n"
        f"│  💰 Total Revenue: **₹{total_rev}**\n"
        f"│  🎁 Daily Free Claims: **{get_today_free_count()}/{MAX_FREE_KEYS_PER_DAY}**\n"
        f"╰━━━━━━━━━━━━━━━━━━━━╯",
        parse_mode="Markdown",
    )


@dp.message(Command("testgen"))
async def cmd_testgen(message: types.Message):
    """Admin test: /testgen 30m"""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Format: `/testgen 30m`", parse_mode="Markdown")
    await message.reply("⏳ Testing @HITXVIPXBOT...")
    key = await generate_from_provider(f"/gen {parts[1]}")
    await message.reply(f"✅ Output:\n`{key}`", parse_mode="Markdown")


# --- STARTUP ---
async def main():
    print("Userbot start ho raha hai...")
    await userbot.start()
    print("Bot & Userbot 24x7 Live hain!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
