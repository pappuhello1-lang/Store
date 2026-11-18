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
ADMIN_ID = 1069319252                                               # Aapka Telegram Numeric ID

# Userbot Credentials (my.telegram.org)
API_ID = 28197998
API_HASH = "190380189baa4e57e8d24fe06b51a6e9"                       # Apna full API_HASH yahan paste karein

# Provider Bot & Channels
PROVIDER_BOT = "@HITXVIPXBOT"
FORCE_JOIN_CHANNEL_ID = -1004445184980
FORCE_JOIN_LINK = "https://t.me/+H8hfyJRq-0EzOTdl"                  # Apne channel ka public/invite link daalein
LOG_GROUP_ID = -1003950310474

# Paytm Business Details
PAYTM_MID = "IWTFev59542546537042"
PAYTM_UPI_ID = "paytm.s3lvd5i@pty"                                  # Apni Paytm Business UPI ID daalein
MERCHANT_NAME = "Dream Events X Sanjeet Flowers"

# Anti-Abuse Settings for Free Key
MAX_FREE_KEYS_PER_DAY = 20  # Ek din mein max kitni free keys nikal sakti hain (balance safety)

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
    m = re.search(r"`([A-Za-z0-9\-_]{5,})`", text)
    if m:
        return m.group(1)
    m = re.search(r"(?:Key|License|Token|Code)\s*[:\-]\s*([A-Za-z0-9\-_]{5,})", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return text.strip()


async def generate_from_provider(cmd_str: str) -> str | None:
    """Userbot ke through @HITXVIPXBOT ko command bhej kar key layega (with entity resolve & debug logs)"""
    async with generation_lock:
        try:
            provider_entity = await userbot.get_entity(PROVIDER_BOT)
        except Exception as e:
            print(f"[ERROR] Provider entity resolve nahi hui ({PROVIDER_BOT}): {e}")
            return None

        future = asyncio.get_running_loop().create_future()

        @userbot.on(events.NewMessage(from_users=provider_entity.id))
        async def handler(event):
            text = event.raw_text or ""
            if "generating" in text.lower() or "wait" in text.lower():
                print(f"[INFO] Provider interim msg: {text}")
                return
            if not future.done():
                future.set_result(text)

        try:
            await userbot.send_message(provider_entity, cmd_str)
            print(f"[DEBUG] Sent '{cmd_str}' to {PROVIDER_BOT} (ID: {provider_entity.id})")
            reply_text = await asyncio.wait_for(future, timeout=20.0)
            print(f"[DEBUG] Provider raw reply:\n{reply_text}")
            return extract_key(reply_text)
        except asyncio.TimeoutError:
            print(f"[ERROR] Timeout! {PROVIDER_BOT} ne 20 second mein reply nahi diya.")
            return None
        except Exception as e:
            print(f"[ERROR] Userbot send/receive failed: {e}")
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
                        f"🎉 **VILLAINSHOP · PAYMENT SUCCESSFUL**\n"
                        f"🆔 Order ID: `{order_id}`\n"
                        f"📦 Plan: **{PLANS[plan]['label']}**\n\n"
                        f"🔑 **YOUR VILLAINSHOP KEY:**\n`{new_key}`",
                        parse_mode="Markdown",
                    )

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
            caption=f"❌ **VILLAINSHOP · PAYMENT WINDOW EXPIRED** (`{order_id}`). Dobara buy karne ke liye /start dabayein.",
            parse_mode="Markdown",
        )
    except Exception:
        pass


# --- MENU & HANDLERS ---
def main_menu_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🎁 FREE 1-HOUR KEY · DAILY", callback_data="claim_free")]
    ]
    for code, info in PLANS.items():
        buttons.append([InlineKeyboardButton(text=info["label"], callback_data=f"buy:{code}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 **WELCOME TO VILLAINSHOP**\n\n"
        "• Choose a plan below for automatic key delivery.\n"
        "• Or claim one free 1-hour key every 24 hours:",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "claim_free")
async def process_free_claim(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if user_id in active_free_claims:
        return await callback.answer("⏳ Aapki request process ho rahi hai, rukiye...", show_alert=True)

    active_free_claims.add(user_id)
    try:
        if not await is_user_joined(user_id):
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📢 JOIN VILLAINSHOP CHANNEL", url=FORCE_JOIN_LINK)],
                    [InlineKeyboardButton(text="✅ Joined! Claim Now", callback_data="claim_free")],
                ]
            )
            await callback.answer()
            return await callback.message.answer(
                "❌ **Free Key Claim karne ke liye pehle humara channel join karein!**",
                reply_markup=kb,
                parse_mode="Markdown",
            )

        now = int(time.time())
        cursor.execute("SELECT last_claimed_at FROM free_claims WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            elapsed = now - row[0]
            if elapsed < 86400:
                rem = 86400 - elapsed
                hrs = rem // 3600
                mins = (rem % 3600) // 60
                return await callback.answer(
                    f"⏳ Aap aaj ki free key le chuke hain!\nDobara {hrs}h {mins}m baad claim karein.",
                    show_alert=True,
                )

        if get_today_free_count() >= MAX_FREE_KEYS_PER_DAY:
            return await callback.answer(
                "⚠️ Aaj ka daily free quota full ho gaya hai! Kal try karein ya paid plan lein.",
                show_alert=True,
            )

        await callback.answer("🎁 Free key generate ho rahi hai...")
        status_msg = await callback.message.answer("⏳ **VILLAINSHOP · GENERATING YOUR FREE KEY**", parse_mode="Markdown")

        free_key = await generate_from_provider("/gen 1h")
        await status_msg.delete()

        if free_key:
            cursor.execute(
                "INSERT INTO free_claims (user_id, last_claimed_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_claimed_at = ?",
                (user_id, now, now),
            )
            conn.commit()
            increment_today_free_count()

            await callback.message.answer(
                f"🎁 **VILLAINSHOP · FREE KEY CLAIMED**\n\n"
                f"🔑 **Key:** `{free_key}`\n"
                f"⏳ Agli free key **24 ghante** baad claim kar sakte hain.",
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
            await callback.message.answer("⚠️ KEY GENERATION TEMPORARILY UNAVAILABLE. Please try again in a minute.")
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
            f"💳 **VILLAINSHOP · SECURE PAYMENT · ₹{amount}**\n"
            f"📦 Plan: **{PLANS[plan]['label']}**\n"
            f"🆔 Order ID: `{order_id}`\n\n"
            f"⏳ Scan with your UPI app and pay the exact amount. Keep this message open; your key is delivered here after payment verification."
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
        f"📊 **VILLAINSHOP · STORE OVERVIEW**\n\n"
        f"💰 Total Paid Orders: **{total_orders}**\n"
        f"💵 Total Revenue: **₹{total_rev}**\n"
        f"🎁 Today's Free Claims: **{get_today_free_count()}/{MAX_FREE_KEYS_PER_DAY}**",
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
    await message.reply("⏳ VILLAINSHOP · TESTING KEY DELIVERY PROVIDER…")
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
