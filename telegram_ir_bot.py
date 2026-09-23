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
TELEGRAM_BOT_TOKEN = "8865575150:AAEwkRhuAi2U_5SE0tgdUX_-HoyFi0NabOA"
ALLOWED_USERS_FILE = "/home/castv/.cc/allowed_users.txt"

# Map Reply Keyboard button labels to exact raw IR parameters or key sequences
# Combos can now use tuples ("KEY", delay_in_ms_after) for customized step timing
IR_KEY_MAP = {
    # Custom Combo Shortcut with precise menu-wait timing
    "❌ MSG": [
        ("BEIN", 2000),   # 2.0 sec wait for beIN menu animation to render
        ("RIGHT", 1000),  # 1.0 sec arrow press
        ("RIGHT", 1000),  # 1.0 sec arrow press
        ("OK", 1500),     # 1.5 sec wait for sub-menu to load
        ("RIGHT", 1000),  # 1.0 sec arrow press
        ("OK", 1500),     # 1.5 sec wait for action execution
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

# Active session store: chat_id -> {"channel": str, "page": Page, "port": str}
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
        [KeyboardButton("❌ MSG")],
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


async def close_chat_session(chat_id: int):
    """Closes and cleans up any open browser tab for a chat session."""
    if chat_id in ACTIVE_SESSIONS:
        session = ACTIVE_SESSIONS.pop(chat_id)
        page: Page = session.get("page")
        if page and not page.is_closed():
            try:
                await page.close()
            except Exception:
                pass


async def open_device_session(chat_id: int, channel_str: str) -> str:
    """Logs into target ESP32 device once and keeps the tab open in background."""
    global browser_obj

    # Close previous active session if running
    await close_chat_session(chat_id)

    try:
        channel_num = int(channel_str)
        port = f"30{channel_num:02d}"
    except ValueError:
        return f"❌ Invalid channel format: `{channel_str}`."

    url = f"http://{BASE_IP}:{port}/login"

    if not browser_obj:
        return "❌ Persistent browser engine not initialized."

    try:
        # Open tab and authenticate
        page = await browser_obj.new_page()
        await page.goto(url, timeout=10000)
        await page.fill("input[name='username']", "admin")
        await page.fill("input[name='password']", "admin123")
        await page.click("button[type='submit']")

        await page.wait_for_url(lambda u: "/login" not in u, timeout=10000)
        await page.wait_for_function("typeof window.ir === 'function'", timeout=10000)

        # Store tab in session memory
        ACTIVE_SESSIONS[chat_id] = {
            "channel": channel_str,
            "page": page,
            "port": port,
        }

        # Send initial channel digit(s) safely via argument passing
        keys = [f"NUM_{digit}" for digit in channel_str]
        for key in keys:
            await page.evaluate("(k) => window.ir(k)", key)
            await page.wait_for_timeout(150)

        return f"✅ Connected to `Device {channel_str}` (Port `{port}`). Sent `{', '.join(keys)}`."

    except Exception as e:
        await close_chat_session(chat_id)
        return f"❌ Error connecting to Device {channel_str} (port {port}): {str(e)}"


async def send_instant_ir_key(chat_id: int, ir_key: str | list, default_delay_ms: int = 1500) -> str:
    """Fires a single IR key or sequential combo sequence with customizable per-step timing."""
    session = ACTIVE_SESSIONS.get(chat_id)

    if not session or not session.get("page"):
        return "⚠️ No active session found. Please select a device using `/chXX` first."

    page: Page = session["page"]
    channel_str = session["channel"]

    if page.is_closed():
        ACTIVE_SESSIONS.pop(chat_id, None)
        return f"⚠️ Session to Device {channel_str} lost. Re-select device with `/ch{channel_str.zfill(2)}`."

    keys_to_send = ir_key if isinstance(ir_key, list) else [ir_key]
    executed_keys = []

    try:
        for idx, item in enumerate(keys_to_send):
            # Parse key name and step delay
            if isinstance(item, (tuple, list)):
                raw_key = item[0]
                step_delay = int(item[1])
            else:
                raw_key = item
                step_delay = default_delay_ms

            # Clean ir('...') string wrappers if present
            clean_key = re.sub(r"^ir\(['\"]?(.*?)['\"]?\)$", r"\1", str(raw_key).strip())

            await page.evaluate("(k) => window.ir(k)", clean_key)
            executed_keys.append(clean_key)

            # Wait delay between sequential commands in combo
            if idx < len(keys_to_send) - 1:
                await page.wait_for_timeout(step_delay)

        if len(executed_keys) > 1:
            return f"⚡ `Device {channel_str}`: Sent Combo `[{', '.join(executed_keys)}]`"
        else:
            return f"⚡ `Device {channel_str}`: Sent `{executed_keys[0]}`"

    except Exception as e:
        return f"❌ Error executing command: {str(e)}"


async def channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles channel commands (/ch01, /ch14) and opens the persistent session."""
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    if not is_user_allowed(user_id, chat_id):
        await update.message.reply_text("⛔ Unauthorized user. Access denied.")
        return

    command_text = update.message.text.strip()
    match = re.match(r"^/ch(\d+)$", command_text, re.IGNORECASE)

    if not match:
        return

    # Strip leading zeros (e.g. "01" -> "1", "14" -> "14")
    new_channel = str(int(match.group(1)))

    await update.message.reply_text(f"⏳ Connecting & opening session for `Device {new_channel}`...", parse_mode="Markdown")

    result = await open_device_session(chat_id, new_channel)

    await update.message.reply_text(
        f"{result}\n\n🎮 **Active Session: `Device {new_channel}`**\nRemote buttons will execute instantly. Send `/close` to exit session.",
        reply_markup=get_reply_keyboard(),
        parse_mode="Markdown",
    )


async def close_session_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Closes active device session, closes browser tab, and removes reply keyboard."""
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    if not is_user_allowed(user_id, chat_id):
        await update.message.reply_text("⛔ Unauthorized user. Access denied.")
        return

    if chat_id in ACTIVE_SESSIONS:
        channel_str = ACTIVE_SESSIONS[chat_id]["channel"]
        await close_chat_session(chat_id)
        await update.message.reply_text(
            f"🔒 Closed session for `Device {channel_str}`. Browser tab closed.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("ℹ️ No active session running.", reply_markup=ReplyKeyboardRemove())


async def reply_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes button presses from the persistent Telegram Reply Keyboard."""
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    if not is_user_allowed(user_id, chat_id):
        await update.message.reply_text("⛔ Unauthorized user. Access denied.")
        return

    button_text = update.message.text.strip()

    if button_text not in IR_KEY_MAP:
        return

    ir_key = IR_KEY_MAP[button_text]
    result = await send_instant_ir_key(chat_id, ir_key)
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

    print("Telegram IR Bot running with Instant Persistent Sessions & Precise ✉️ MSG Combo...")
    app.run_polling()


if __name__ == "__main__":
    main()
