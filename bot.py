"""
Steam TG Bot — Web Service (webhook + Flask).
Render Web Service:
  Build:  pip install -r requirements.txt
  Start:  gunicorn -b 0.0.0.0:$PORT bot:app
Env:
  BOT_TOKEN   — 9194874168:AAGnsgFQhXzwVccg52aJsHlHFhpmZCtEhbs
  WEBHOOK_URL — https://steam-tg-bot-zvs6.onrender.com  (без / в конце)
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path

import telebot
from flask import Flask, request
from telebot import types

from steam_check_sync import check_steam

ROOT = Path(__file__).resolve().parent
ACCOUNTS = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
PAGE_SIZE = 5

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not set")

# https://steam-tg-bot.onrender.com  — без слэша в конце
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)
user_results: dict[int, list] = {}


def esc(s: str) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def search_accounts(query: str) -> list:
    q = (query or "").strip().lower()
    if len(q) < 2:
        return []
    out = []
    for a in ACCOUNTS:
        if q in (a.get("l") or "").lower() or q in (a.get("g") or "").lower():
            out.append(a)
            if len(out) >= 200:
                break
    return out


def page_kb(uid: int, page: int) -> types.InlineKeyboardMarkup:
    found = user_results.get(uid) or []
    pages = max(1, (len(found) + PAGE_SIZE - 1) // PAGE_SIZE)
    kb = types.InlineKeyboardMarkup()
    start = page * PAGE_SIZE
    for i, a in enumerate(found[start : start + PAGE_SIZE]):
        idx = start + i
        short = (a.get("g") or a.get("l") or "?")[:32]
        kb.add(types.InlineKeyboardButton(f"{idx + 1}. {short}", callback_data=f"acc:{idx}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("←", callback_data=f"page:{page - 1}"))
    nav.append(types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(types.InlineKeyboardButton("→", callback_data=f"page:{page + 1}"))
    if nav:
        kb.row(*nav)
    return kb


def acc_kb(idx: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🔑 Логин", callback_data=f"login:{idx}"),
        types.InlineKeyboardButton("🔐 Пароль", callback_data=f"pass:{idx}"),
    )
    kb.add(types.InlineKeyboardButton("✅ Проверить", callback_data=f"check:{idx}"))
    kb.add(types.InlineKeyboardButton("« К списку", callback_data="page:0"))
    return kb


def fmt_acc(a: dict, n: int | None = None) -> str:
    head = f"<b>#{n}</b>\n" if n is not None else ""
    return (
        f"{head}🎮 <b>{esc(a.get('g') or '—')}</b>\n"
        f"👤 <code>{esc(a.get('l'))}</code>\n"
        f"🔑 <code>{esc(a.get('p'))}</code>"
    )


@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    bot.reply_to(
        message,
        f"Steam Accounts Bot\nВсего: <b>{len(ACCOUNTS)}</b>\n\n"
        f"/find <i>игра</i> — поиск\n"
        f"/check логин пароль — проверка\n"
        f"/random — случайный\n"
        f"Или просто напиши название игры.",
    )


@bot.message_handler(commands=["random"])
def cmd_random(message: types.Message):
    a = random.choice(ACCOUNTS)
    user_results[message.from_user.id] = [a]
    bot.reply_to(message, fmt_acc(a, 1), reply_markup=acc_kb(0))


@bot.message_handler(commands=["find"])
def cmd_find(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    q = parts[1] if len(parts) > 1 else ""
    if len(q) < 2:
        bot.reply_to(message, "Пример: <code>/find forza</code>")
        return
    found = search_accounts(q)
    if not found:
        bot.reply_to(message, f"Ничего не найдено: <b>{esc(q)}</b>")
        return
    uid = message.from_user.id
    user_results[uid] = found
    bot.reply_to(
        message,
        f"Найдено: <b>{len(found)}</b>\nВыбери аккаунт:",
        reply_markup=page_kb(uid, 0),
    )


@bot.message_handler(commands=["check"])
def cmd_check(message: types.Message):
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "Формат: <code>/check логин пароль</code>")
        return
    login, password = parts[1], parts[2]
    wait = bot.reply_to(message, "⏳ Проверяю Steam...")
    result = check_steam(login, password)
    status = result.get("status")
    msg = result.get("message") or status
    if status == "success":
        text = f"✅ <b>{esc(msg)}</b>\n👤 <code>{esc(login)}</code>"
    elif status == "captcha":
        text = f"⚠️ {esc(msg)}"
    else:
        text = f"❌ <b>{esc(msg)}</b>\n👤 <code>{esc(login)}</code>"
    bot.edit_message_text(text, message.chat.id, wait.message_id)


@bot.callback_query_handler(func=lambda c: True)
def on_callback(cq: types.CallbackQuery):
    data = cq.data or ""
    uid = cq.from_user.id
    found = user_results.get(uid) or []

    if data == "noop":
        bot.answer_callback_query(cq.id)
        return

    if data.startswith("page:"):
        page = int(data.split(":")[1])
        if not found:
            bot.answer_callback_query(cq.id, "Сначала /find", show_alert=True)
            return
        bot.edit_message_reply_markup(
            cq.message.chat.id, cq.message.message_id, reply_markup=page_kb(uid, page)
        )
        bot.answer_callback_query(cq.id)
        return

    if data.startswith("acc:"):
        idx = int(data.split(":")[1])
        if idx < 0 or idx >= len(found):
            bot.answer_callback_query(cq.id, "Нет", show_alert=True)
            return
        bot.send_message(cq.message.chat.id, fmt_acc(found[idx], idx + 1), reply_markup=acc_kb(idx))
        bot.answer_callback_query(cq.id)
        return

    if data.startswith("login:") or data.startswith("pass:"):
        kind, idx_s = data.split(":")
        idx = int(idx_s)
        if idx >= len(found):
            bot.answer_callback_query(cq.id, "Нет", show_alert=True)
            return
        val = found[idx].get("l" if kind == "login" else "p")
        bot.send_message(cq.message.chat.id, f"<code>{esc(val)}</code>")
        bot.answer_callback_query(cq.id)
        return

    if data.startswith("check:"):
        idx = int(data.split(":")[1])
        if idx >= len(found):
            bot.answer_callback_query(cq.id, "Нет", show_alert=True)
            return
        a = found[idx]
        bot.answer_callback_query(cq.id, "Проверяю...")
        wait = bot.send_message(cq.message.chat.id, "⏳ Проверка Steam...")
        result = check_steam(a.get("l") or "", a.get("p") or "")
        status = result.get("status")
        msg = result.get("message") or status
        if status == "success":
            text = f"✅ <b>{esc(msg)}</b>\n👤 <code>{esc(a.get('l'))}</code>"
        elif status == "captcha":
            text = f"⚠️ {esc(msg)}"
        else:
            text = f"❌ <b>{esc(msg)}</b>"
        bot.edit_message_text(text, cq.message.chat.id, wait.message_id)
        return

    bot.answer_callback_query(cq.id)


@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith("/"))
def on_text(message: types.Message):
    q = (message.text or "").strip()
    if len(q) < 2:
        return
    found = search_accounts(q)
    if not found:
        bot.reply_to(message, f"Ничего не найдено: <b>{esc(q)}</b>")
        return
    uid = message.from_user.id
    user_results[uid] = found
    bot.reply_to(
        message,
        f"Найдено: <b>{len(found)}</b>\nВыбери аккаунт:",
        reply_markup=page_kb(uid, 0),
    )


# ---------- HTTP (Web Service) ----------

@app.get("/")
def health():
    return {
        "ok": True,
        "accounts": len(ACCOUNTS),
        "webhook": bool(WEBHOOK_URL),
    }, 200


@app.post("/webhook")
def telegram_webhook():
    if request.headers.get("content-type", "").startswith("application/json"):
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
        return "", 200
    return "bad request", 400


def setup_webhook():
    if not WEBHOOK_URL:
        print("WARNING: WEBHOOK_URL not set — set it to https://your-service.onrender.com")
        return
    url = f"{WEBHOOK_URL}/webhook"
    bot.remove_webhook()
    ok = bot.set_webhook(url=url)
    print(f"Webhook set: {url} -> {ok}")


setup_webhook()
print(f"Web service ready | accounts: {len(ACCOUNTS)} | PORT={PORT}")

if __name__ == "__main__":
    # local test
    app.run(host="0.0.0.0", port=PORT)
