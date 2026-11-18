#!/usr/bin/env python3
"""Telegram launcher for the Football Intelligence Mini App."""
import os
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def load_env():
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
try:
    ALLOWED_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0"))
except ValueError:
    ALLOWED_ID = 0

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing from .env")
if not ALLOWED_ID:
    raise RuntimeError("TELEGRAM_ALLOWED_USER_ID is missing from .env")


def allowed(update: Update):
    return bool(update.effective_user and update.effective_user.id == ALLOWED_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("Access denied. This is a private app.")
        return

    if not MINI_APP_URL.startswith("https://"):
        await update.effective_message.reply_text(
            "Your Mini App launcher is ready, but MINI_APP_URL is not configured.\n\n"
            "Add your public HTTPS Mini App URL to .env:\n"
            "MINI_APP_URL=https://your-domain.example\n\n"
            "Then restart this bot."
        )
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "⚽ OPEN FOOTBALL PREDICTIONS",
            web_app=WebAppInfo(url=MINI_APP_URL),
        )
    ]])
    await update.effective_message.reply_text(
        "⚽ FOOTBALL INTELLIGENCE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Your football prediction Mini App is ready.\n\n"
        "Open the app below and press Predict Matches when you want a new analysis.",
        reply_markup=keyboard,
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Your Telegram ID: {update.effective_user.id}"
    )


def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myid", myid))
    print("Football Intelligence Telegram Mini App launcher is running.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
