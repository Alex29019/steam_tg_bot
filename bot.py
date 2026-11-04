"""
Steam accounts list bot for groups.
/accfree [игра]
Каждые 30 мин — напоминание в добавленные чаты.
Start: gunicorn -b 0.0.0.0:$PORT bot:app
Env: BOT_TOKEN, WEBHOOK_URL
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import telebot
from flask import Flask, request
from telebot import types

ROOT = Path(__file__).resolve().parent
ACCOUNTS = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
CHATS_FILE = ROOT / "known_chats.json"
PAGE_SIZE = 10
CLICK_COOLDOWN = 5
REMINDER_INTERVAL = 30 * 60  # 30 минут
REMINDER_TEXT = (
    "Напомню!!!\n"
    "Я бот для поиска аккаунтов, напиши команду /accfree "
    "что бы получить доступ к 2к+ акков"
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not set")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

user_results: dict[str, dict] = {}
last_click: dict[int, float] = {}
last_cmd: dict[int, float] = {}


def load_chats() -> set[int]:
    try:
        if CHATS_FILE.exists():
            data = json.loads(CHATS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {int(x) for x in data}
    except Exception as e:
        print("load_chats:", e)
    return set()


def save_chats(chats: set[int]) -> None:
    try:
        CHATS_FILE.write_text(
            json.dumps(sorted(chats), ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print("save_chats:", e)


known_chats: set[int] = load_chats()


def remember_chat(chat_id: int, chat_type: str | None = None) -> None:
    """Запомнить группу/супергруппу для рассылки."""
    if chat_type and chat_type not in ("group", "supergroup"):
        return
    if chat_id in known_chats:
        return
    known_chats.add(chat_id)
    save_chats(known_chats)
    print(f"remember chat {chat_id}, total={len(known_chats)}")


def forget_chat(chat_id: int) -> None:
    if chat_id in known_chats:
        known_chats.discard(chat_id)
        save_chats(known_chats)
        print(f"forget chat {chat_id}")


def ctx_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def esc(s: str) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def search_accounts(query: str | None = None) -> list:
    q = (query or "").strip().lower()
    if not q:
        return list(ACCOUNTS)
    out = []
    for a in ACCOUNTS:
        if q in (a.get("l") or "").lower() or q in (a.get("g") or "").lower():
            out.append(a)
            if len(out) >= 500:
                break
    return out


def page_kb(key: str, page: int) -> types.InlineKeyboardMarkup:
    data = user_results.get(key) or {"list": []}
    found = data.get("list") or []
    pages = max(1, (len(found) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    kb = types.InlineKeyboardMarkup()
    start = page * PAGE_SIZE
    for i, a in enumerate(found[start : start + PAGE_SIZE]):
        idx = start + i
        games = a.get("g") or a.get("l") or "?"
        short = games if len(games) <= 40 else games[:37] + "..."
        kb.add(
            types.InlineKeyboardButton(
                f"{idx + 1}. {short}", callback_data=f"a:{idx}:{page}"
            )
        )
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("←", callback_data=f"p:{page - 1}"))
    nav.append(types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(types.InlineKeyboardButton("→", callback_data=f"p:{page + 1}"))
    if nav:
        kb.row(*nav)
    return kb


def fmt_acc(a: dict, n: int | None = None) -> str:
    head = f"<b>#{n}</b>\n" if n is not None else ""
    return (
        f"{head}🎮 <b>{esc(a.get('g') or '—')}</b>\n\n"
        f"👤 Логин:\n<code>{esc(a.get('l'))}</code>\n\n"
        f"🔑 Пароль:\n<code>{esc(a.get('p'))}</code>"
    )


def show_menu(
    chat_id: int,
    user_id: int,
    query: str | None,
    reply_to: types.Message | None = None,
    chat_type: str | None = None,
):
    if chat_type:
        remember_chat(chat_id, chat_type)
    elif chat_id < 0:
        remember_chat(chat_id, "supergroup")

    found = search_accounts(query)
    key = ctx_key(chat_id, user_id)
    user_results[key] = {"list": found, "q": (query or "").strip()}
    if not found:
        text = f"Ничего не найдено по «{esc(query or '')}»"
        if reply_to:
            bot.reply_to(reply_to, text)
        else:
            bot.send_message(chat_id, text)
        return
    q = (query or "").strip()
    title = (
        f"📂 <b>Бесплатные аккаунты</b>\nНайдено: <b>{len(found)}</b> по «{esc(q)}»"
        if q
        else (
            f"📂 <b>Бесплатные аккаунты</b>\nВсего: <b>{len(found)}</b>\n"
            f"Поиск: <code>/accfree название игры</code>"
        )
    )
    kb = page_kb(key, 0)
    if reply_to:
        bot.reply_to(reply_to, title, reply_markup=kb)
    else:
        bot.send_message(chat_id, title, reply_markup=kb)


def send_reminders() -> None:
    chats = list(load_chats())
    print(f"reminder tick, chats={len(chats)}")
    for cid in chats:
        try:
            bot.send_message(cid, REMINDER_TEXT)
            time.sleep(0.05)
        except Exception as e:
            err = str(e).lower()
            print(f"reminder fail {cid}: {e}")
            if any(
                x in err
                for x in (
                    "blocked",
                    "kicked",
                    "deactivated",
                    "chat not found",
                    "forbidden",
                    "have no rights",
                )
            ):
                forget_chat(cid)


def reminder_loop() -> None:
    # первая пауза 30 мин после старта, потом каждые 30 мин
    while True:
        time.sleep(REMINDER_INTERVAL)
        try:
            send_reminders()
        except Exception as e:
            print("reminder_loop error:", e)


@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    remember_chat(message.chat.id, message.chat.type)
    bot.reply_to(
        message,
        "Список общих Steam-аккаунтов.\n\n"
        "<b>/accfree</b> — все аккаунты\n"
        "<b>/accfree forza</b> — поиск по игре\n\n"
        "Работает в личке и в группах.",
    )


@bot.message_handler(commands=["accfree"])
def cmd_accfree(message: types.Message):
    uid = message.from_user.id
    now = time.time()
    left = CLICK_COOLDOWN - (now - last_cmd.get(uid, 0))
    if left > 0:
        bot.reply_to(message, f"⏳ Подожди ещё <b>{int(left) + 1}</b> сек.")
        return
    last_cmd[uid] = now
    raw = message.text or ""
    parts = raw.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    show_menu(
        message.chat.id,
        uid,
        query or None,
        reply_to=message,
        chat_type=message.chat.type,
    )


@bot.my_chat_member_handler()
def on_my_chat_member(message: types.ChatMemberUpdated):
    """Когда бота добавили / кикнули из чата."""
    try:
        chat = message.chat
        new = message.new_chat_member
        status = (new.status if new else "") or ""
        if status in ("member", "administrator"):
            remember_chat(chat.id, chat.type)
            print(f"bot added to {chat.id} ({chat.type})")
        elif status in ("left", "kicked"):
            forget_chat(chat.id)
            print(f"bot removed from {chat.id}")
    except Exception as e:
        print("my_chat_member error:", e)


@bot.callback_query_handler(func=lambda c: True)
def on_callback(cq: types.CallbackQuery):
    data = cq.data or ""
    uid = cq.from_user.id
    chat_id = cq.message.chat.id if cq.message else 0

    if data != "noop":
        now = time.time()
        prev = last_click.get(uid, 0)
        left = CLICK_COOLDOWN - (now - prev)
        if left > 0:
            bot.answer_callback_query(
                cq.id, f"Подожди {int(left) + 1} сек.", show_alert=False
            )
            return
        last_click[uid] = now

    if cq.message:
        remember_chat(chat_id, cq.message.chat.type)

    key = ctx_key(chat_id, uid)
    found_data = user_results.get(key)
    if not found_data:
        for k, v in user_results.items():
            if k.endswith(f":{uid}"):
                found_data = v
                key = k
                break
    found = (found_data or {}).get("list") or []

    if data == "noop":
        bot.answer_callback_query(cq.id)
        return

    if data.startswith("p:"):
        try:
            page = int(data.split(":")[1])
        except Exception:
            bot.answer_callback_query(cq.id)
            return
        if not found:
            bot.answer_callback_query(cq.id, "Снова /accfree", show_alert=True)
            return
        try:
            bot.edit_message_reply_markup(
                chat_id, cq.message.message_id, reply_markup=page_kb(key, page)
            )
        except Exception:
            bot.send_message(chat_id, "Список:", reply_markup=page_kb(key, page))
        bot.answer_callback_query(cq.id)
        return

    if data.startswith("a:"):
        parts = data.split(":")
        try:
            idx = int(parts[1])
        except Exception:
            bot.answer_callback_query(cq.id)
            return
        if idx < 0 or idx >= len(found):
            bot.answer_callback_query(cq.id, "Снова /accfree", show_alert=True)
            return
        a = found[idx]
        bot.send_message(chat_id, fmt_acc(a, idx + 1))
        bot.answer_callback_query(cq.id)
        return

    bot.answer_callback_query(cq.id)


@app.get("/")
def health():
    return {
        "ok": True,
        "accounts": len(ACCOUNTS),
        "chats": len(known_chats),
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
        print("WARNING: WEBHOOK_URL not set")
        return
    url = f"{WEBHOOK_URL}/webhook"
    try:
        bot.remove_webhook()
        ok = bot.set_webhook(url=url)
        print(f"Webhook set: {url} -> {ok}")
    except Exception as e:
        print(f"Webhook error: {e}")


setup_webhook()

# фоновый поток напоминаний
_t = threading.Thread(target=reminder_loop, name="reminder", daemon=True)
_t.start()

print(f"Ready | accounts={len(ACCOUNTS)} | known_chats={len(known_chats)}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
