# COMMIT TEST
import asyncio
import os
import re
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from playwright.async_api import async_playwright, Browser, Playwright, Page

BASE_IP = "dz.ps.ai"
TELEGRAM_BOT_TOKEN = ""
ALLOWED_USERS_FILE = "/home/castv/.cc/allowed_users.txt"
IDLE_TIMEOUT_SECONDS = 30  # Auto-close idle sessions after 30 seconds

# Centralized Customizable Telegram Messages
MESSAGES = {
    "UNAUTHORIZED": "⛔ Access denied.",
    "CONNECTING": "⏳ Connecting to `beIN {channel}`...",
    "CONNECTED": "✅ Connected to `beIN {channel}`",
    "KEY_SENT": "⚡ `beIN {channel}` ➔ IR {key} Sent",
    "COMBO_SENT": "🚀 `beIN {channel}` ➔ Bmail Removed",
    "NO_SESSION": "⚠️ No active session found. Please select STB first.",
    "SESSION_CLOSED": "🔒 Session for `beIN {channel}` has been closed successfully.",
    "SESSION_TIMEOUT": "⏱️ Session for `beIN {channel}` closed automatically due to 30s inactivity.",
    "NO_ACTIVE_SESSION": "ℹ️ No active session running.",
    "SESSION_LOST": "⚠️ Session to `beIN {channel}` lost. Re-select STB.",
    "ERROR": "❌ Error on `beIN {channel}`: {error}",
}

# Map Reply Keyboard button labels to exact raw IR parameters or key sequences
IR_KEY_MAP = {
    # Custom Combo Shortcut with precise menu timing
    "✉️ MSG": [
        ("BEIN", 2000),   # 2.0 sec wait for beIN menu animation
        ("RIGHT", 1000),  # 1.0 sec arrow press
        ("RIGHT", 1000),  # 1.0 sec arrow press
        ("OK", 1500),     # 1.5 sec wait for sub-menu
        ("RIGHT", 1000),  # 1.0 sec arrow press
        ("OK", 1500),     # 1.5 sec wait for action
        ("EXIT", 1000),   # 1.0 sec exit menu
    ],
    # System & Audio
    "🔴 Power": "ir('POWER')",
    "⚙️ beIN": "ir('BEIN')",
    "🔊 Vol +": "ir('VOL_UP')",
    "🔉 Vol -": "ir('VOL_DOWN')",
    # Number Pad
    "1": "ir('NUM_1')",
    "2": "ir('NUM_2')",
    "3": "ir('NUM_3')",
    "4": "ir('NUM_4')",
    "5": "ir('NUM_5')",
    "6": "ir('NUM_6')",
    "7": "ir('NUM_7')",
    "8": "ir('NUM_8')",
    "9": "ir('NUM_9')",
    "0": "ir('NUM_0')",
    # Navigation
    "⬆️ Up": "ir('UP')",
    "⬇️ Down": "ir('DOWN')",
    "⬅️ Left": "ir('LEFT')",
    "➡️ Right": "ir('RIGHT')",
    "🔘 OK": "ir('OK')",
    "🔙 Back": "ir('BACK')",
    "⚡ EXIT": "ir('EXIT')",
}

# Global persistent browser state
playwright_obj: Playwright = None
browser_obj: Browser = None

# Active session store: chat_id -> {"channel": str, "page": Page, "port": str, "timer_task": Task}
ACTIVE_SESSIONS: dict[int, dict] = {}


def is_user_allowed(user_id: int, chat_id: int) -> bool:
    """Reads allowed user/chat IDs dynamically from whitelist file."""
    if not os.path.exists(ALLOWED_USERS_FILE):
        return False

    allowed_ids = set()
    try:
        with open(ALLOWED_USERS_FILE, "r") as f:
            for line in f:
                clean_line = line.split("#")[0].strip()
                if clean_line:
                    allowed_ids.add(clean_line)
    except Exception:
        return False

    return str(user_id) in allowed_ids or str(chat_id) in allowed_ids


def get_reply_keyboard() -> ReplyKeyboardMarkup:
    """Generates the remote control grid with numbers, navigation, and MSG combo."""
    keyboard = [
        [KeyboardButton("✉️ MSG")],
        [KeyboardButton("🔴 Power"), KeyboardButton("⚙️ beIN"), KeyboardButton("🔊 Vol +"), KeyboardButton("🔉 Vol -")],
        [KeyboardButton("1"), KeyboardButton("2"), KeyboardButton("3")],
        [KeyboardButton("4"), KeyboardButton("5"), KeyboardButton("6")],
        [KeyboardButton("7"), KeyboardButton("8"), KeyboardButton("9")],
        [KeyboardButton("🔙 Back"), KeyboardButton("0"), KeyboardButton("⚡ EXIT")],
        [KeyboardButton("⬆️ Up")],
        [KeyboardButton("⬅️ Left"), KeyboardButton("🔘 OK"), KeyboardButton("➡️ Right")],
        [KeyboardButton("⬇️ Down")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def close_chat_session(chat_id: int) -> str | None:
    """Closes browser tab and timer for a chat session without cancelling itself."""
    if chat_id in ACTIVE_SESSIONS:
        session = ACTIVE_SESSIONS.pop(chat_id)
        channel_str = session.get("channel")
        
        # Cancel running timeout task ONLY if it is not the currently executing task
        timer_task = session.get("timer_task")
        current_task = asyncio.current_task()
        if timer_task and timer_task != current_task and not timer_task.done():
            timer_task.cancel()

        page: Page = session.get("page")
        if page and not page.is_closed():
            try:
                await page.close()
            except Exception:
                pass

        return channel_str
    return None


async def auto_close_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Waits for timeout duration and sends an auto-close confirmation message if idle."""
    try:
        await asyncio.sleep(IDLE_TIMEOUT_SECONDS)
        channel_str = await close_chat_session(chat_id)
        if channel_str:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=MESSAGES["SESSION_TIMEOUT"].format(channel=channel_str),
                    reply_markup=ReplyKeyboardRemove(),
                    parse_mode="Markdown",
                )
            except Exception as e:
                print(f"Error sending timeout notification to chat {chat_id}: {e}")
    except asyncio.CancelledError:
        pass  # Timer was reset by new user activity


def reset_inactivity_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Resets the idle countdown task for an active session."""
    if chat_id in ACTIVE_SESSIONS:
        session = ACTIVE_SESSIONS[chat_id]
        old_timer = session.get("timer_task")
        if old_timer and not old_timer.done():
            old_timer.cancel()

        session["timer_task"] = asyncio.create_task(auto_close_timer(chat_id, context))


async def open_device_session(chat_id: int, channel_str: str) -> str:
    """Logs into target ESP32 device once and keeps the tab open in background."""
    global browser_obj

    # Clean up previous session cleanly
    await close_chat_session(chat_id)

    try:
        channel_num = int(channel_str)
        port = f"30{channel_num:02d}"
    except ValueError:
        return MESSAGES["ERROR"].format(channel=channel_str, error="Invalid channel format")

    url = f"http://{BASE_IP}:{port}/login"

    if not browser_obj:
        return "❌ Persistent browser engine not initialized."

    try:
        page = await browser_obj.new_page()
        await page.goto(url, timeout=10000)
        await page.fill("input[name='username']", "admin")
        await page.fill("input[name='password']", "admin123")
        await page.click("button[type='submit']")

        await page.wait_for_url(lambda u: "/login" not in u, timeout=10000)
        await page.wait_for_function("typeof window.ir === 'function'", timeout=10000)

        ACTIVE_SESSIONS[chat_id] = {
            "channel": channel_str,
            "page": page,
            "port": port,
            "timer_task": None,
        }

        keys = [f"NUM_{digit}" for digit in channel_str]
        for key in keys:
            await page.evaluate("(k) => window.ir(k)", key)
            await page.wait_for_timeout(150)

        return MESSAGES["CONNECTED"].format(channel=channel_str, port=port)

    except Exception as e:
        await close_chat_session(chat_id)
        return MESSAGES["ERROR"].format(channel=channel_str, error=str(e))


async def send_instant_ir_key(chat_id: int, ir_key: str | list, default_delay_ms: int = 1500) -> str:
    """Fires a single IR key or sequential combo sequence with customizable per-step timing."""
    session = ACTIVE_SESSIONS.get(chat_id)

    if not session or not session.get("page"):
        return MESSAGES["NO_SESSION"]

    page: Page = session["page"]
    channel_str = session["channel"]

    if page.is_closed():
        await close_chat_session(chat_id)
        return MESSAGES["SESSION_LOST"].format(channel=channel_str.zfill(2))

    keys_to_send = ir_key if isinstance(ir_key, list) else [ir_key]
    executed_keys = []

    try:
        for idx, item in enumerate(keys_to_send):
            if isinstance(item, (tuple, list)):
                raw_key, step_delay = item[0], int(item[1])
            else:
                raw_key, step_delay = item, default_delay_ms

            clean_key = re.sub(r"^ir\(['\"]?(.*?)['\"]?\)$", r"\1", str(raw_key).strip())

            await page.evaluate("(k) => window.ir(k)", clean_key)

            display_key = re.sub(r"^NUM_", "", clean_key)
            executed_keys.append(display_key)

            if idx < len(keys_to_send) - 1:
                await page.wait_for_timeout(step_delay)

        if len(executed_keys) > 1:
            return MESSAGES["COMBO_SENT"].format(channel=channel_str, keys=", ".join(executed_keys))
        else:
            return MESSAGES["KEY_SENT"].format(channel=channel_str, key=executed_keys[0])

    except Exception as e:
        return MESSAGES["ERROR"].format(channel=channel_str, error=str(e))


async def channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles channel commands (/ch01, /ch14) and opens the persistent session."""
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    if not is_user_allowed(user_id, chat_id):
        await update.message.reply_text(MESSAGES["UNAUTHORIZED"])
        return

    command_text = update.message.text.strip()
    match = re.match(r"^/ch(\d+)$", command_text, re.IGNORECASE)

    if not match:
        return

    new_channel = str(int(match.group(1)))

    connecting_msg = await update.message.reply_text(
        MESSAGES["CONNECTING"].format(channel=new_channel), 
        parse_mode="Markdown"
    )

    result = await open_device_session(chat_id, new_channel)

    try:
        await connecting_msg.delete()
    except Exception:
        pass

    reset_inactivity_timer(chat_id, context)

    await update.message.reply_text(
        result,
        reply_markup=get_reply_keyboard(),
        parse_mode="Markdown",
    )


async def close_session_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Closes active device session, removes keyboard, and replies with explicit confirmation."""
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    if not is_user_allowed(user_id, chat_id):
        await update.message.reply_text(MESSAGES["UNAUTHORIZED"])
        return

    closed_channel = await close_chat_session(chat_id)

    if closed_channel:
        await update.message.reply_text(
            MESSAGES["SESSION_CLOSED"].format(channel=closed_channel),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            MESSAGES["NO_ACTIVE_SESSION"], 
            reply_markup=ReplyKeyboardRemove()
        )


async def reply_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes button presses from the persistent Telegram Reply Keyboard."""
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    if not is_user_allowed(user_id, chat_id):
        await update.message.reply_text(MESSAGES["UNAUTHORIZED"])
        return

    button_text = update.message.text.strip()

    if button_text not in IR_KEY_MAP:
        return

    ir_key = IR_KEY_MAP[button_text]
    result = await send_instant_ir_key(chat_id, ir_key)

    # Reset 30s timeout on button interaction
    reset_inactivity_timer(chat_id, context)

    await update.message.reply_text(result, parse_mode="Markdown")


async def post_init(application):
    """Launches Playwright Chromium engine on bot startup."""
    global playwright_obj, browser_obj
    playwright_obj = await async_playwright().start()
    browser_obj = await playwright_obj.chromium.launch(headless=True)
    print("⚡ Persistent Chromium Engine initialized successfully.")


async def post_shutdown(application):
    """Cleanly closes open sessions and Playwright engine on bot shutdown."""
    global playwright_obj, browser_obj
    for chat_id in list(ACTIVE_SESSIONS.keys()):
        await close_chat_session(chat_id)
    if browser_obj:
        await browser_obj.close()
    if playwright_obj:
        await playwright_obj.stop()
    print("🔒 Browser Engine cleanly shutdown.")


def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("close", close_session_handler))
    app.add_handler(MessageHandler(filters.Regex(r"^/ch\d+$"), channel_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_button_handler))

    print("Telegram IR Bot running with fixed auto-close notifications...")
    app.run_polling()


if __name__ == "__main__":
    main()
