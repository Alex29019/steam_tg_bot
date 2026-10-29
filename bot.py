
"""
Steam TG Bot — Temporary Maintenance Mode
Start: gunicorn -b 0.0.0.0:$PORT bot:app

Environment:
BOT_TOKEN  - Telegram bot token
WEBHOOK_URL - public HTTPS URL of this service
PORT - optional, defaults to 10000
"""

import os
import telebot
from flask import Flask, request
from telebot import types

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not set")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

NOTICE = (
    "⚠️ <b>Бот временно остановлен</b>\n\n"
    "В связи с баганным кодом бот временно недоступен.\n"
    "Скоро всё починю! 🔧\n\n"
    "Пока можешь воспользоваться кнопками ниже 👇"
)


def maintenance_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)

    kb.add(
        types.InlineKeyboardButton(
            "📢 Подпишись на ТГ",
            url="https://t.me/MirArenDaff"
        ),
        types.InlineKeyboardButton(
            "🌐 Мой сайт",
            url="https://steam-checker-fr12.onrender.com/"
        )
    )

    return kb


def send_notice(chat_id):
    bot.send_message(
        chat_id,
        NOTICE,
        reply_markup=maintenance_keyboard(),
        parse_mode="HTML"
    )


# Любые команды, включая /start, /help, /check и /add.
@bot.message_handler(commands=None, func=lambda message: True)
def maintenance_message(message):
    send_notice(message.chat.id)


# Старые кнопки тоже не запускают функции.
@bot.callback_query_handler(func=lambda call: True)
def maintenance_callback(call):
    try:
        bot.answer_callback_query(
            call.id,
            "Бот временно остановлен."
        )
    except Exception:
        pass

    try:
        send_notice(call.message.chat.id)
    except Exception:
        pass


@app.get("/")
def health():
    return {
        "ok": True,
        "mode": "maintenance"
    }, 200


@app.post("/webhook")
def telegram_webhook():
    if not request.is_json:
        return "bad request", 400

    try:
        update = telebot.types.Update.de_json(
            request.get_data().decode("utf-8")
        )
        bot.process_new_updates([update])
        return "", 200
    except Exception as exc:
        app.logger.error("Webhook processing failed: %s", exc)
        return "processing error", 500


def setup_webhook():
    if not WEBHOOK_URL:
        print("WARNING: WEBHOOK_URL not set")
        return

    url = f"{WEBHOOK_URL}/webhook"

    try:
        bot.remove_webhook()
        result = bot.set_webhook(url=url)
        print(f"Webhook set: {url} -> {result}")
    except Exception as exc:
        print(f"Webhook error: {exc}")


setup_webhook()
print("Bot started in maintenance mode.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
