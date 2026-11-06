"""
Steam accounts list bot for groups.
/accfree [игра]
Админ: секрет из Env ADMIN_SECRET (не светить в чате и не коммитить).
Start: gunicorn -b 0.0.0.0:$PORT bot:app
Env: BOT_TOKEN, WEBHOOK_URL, ADMIN_SECRET, optional ADMIN_IDS=1,2,3
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets as py_secrets
import threading
import time
from pathlib import Path

import telebot
from flask import Flask, request
from telebot import types

ROOT = Path(__file__).resolve().parent
ACCOUNTS = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
CHATS_FILE = ROOT / "known_chats.json"
USERS_FILE = ROOT / "chat_users.json"
SETTINGS_FILE = ROOT / "bot_settings.json"
SESSIONS_FILE = ROOT / "admin_sessions.json"
STATE_FILE = ROOT / "bot_state.json"  # <-- НОВОЕ: единый файл состояния

PAGE_SIZE = 10
CLICK_COOLDOWN = 5

_ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

_ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not set")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# Runtime state (теперь персистентный)
user_results: dict[str, dict] = {}
last_click: dict[int, float] = {}
last_cmd: dict[int, float] = {}

admin_sessions: set[int] = set()
admin_wait: dict[int, str] = {}
admin_selected: dict[int, list[int]] = {}  # set не сериализуется — используем list

chat_title_cache: dict[int, str] = {}


# ===== ПЕРСИСТЕНТНОСТЬ =====

def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print("load_json", path, e)
    return default


def save_json(path: Path, data) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("save_json", path, e)


def save_state() -> None:
    """Сохраняет всё runtime-состояние в один файл."""
    try:
        state = {
            "user_results": {k: v for k, v in user_results.items()},
            "last_click": {str(k): v for k, v in last_click.items()},
            "last_cmd": {str(k): v for k, v in last_cmd.items()},
            "admin_wait": {str(k): v for k, v in admin_wait.items()},
            "admin_selected": {str(k): list(v) for k, v in admin_selected.items()},
            "admin_sessions": sorted(admin_sessions),
        }
        save_json(STATE_FILE, state)
    except Exception as e:
        print("save_state error:", e)


def load_state() -> None:
    """Восстанавливает состояние из файла при старте."""
    global user_results, last_click, last_cmd, admin_wait, admin_selected, admin_sessions
    try:
        state = load_json(STATE_FILE, {})
        if not isinstance(state, dict):
            return

        user_results = state.get("user_results", {}) or {}

        last_click = {int(k): float(v) for k, v in (state.get("last_click") or {}).items()}
        last_cmd = {int(k): float(v) for k, v in (state.get("last_cmd") or {}).items()}

        # Очищаем устаревшие КД (старше 5 минут) — чтобы не блокировать пользователей после рестарта
        now = time.time()
        last_click = {k: v for k, v in last_click.items() if now - v < 300}
        last_cmd = {k: v for k, v in last_cmd.items() if now - v < 300}

        admin_wait = {int(k): v for k, v in (state.get("admin_wait") or {}).items()}
        admin_selected = {int(k): set(v) for k, v in (state.get("admin_selected") or {}).items()}
        admin_sessions = set(state.get("admin_sessions", []) or [])

        print(f"State restored: sessions={len(admin_sessions)}, wait={len(admin_wait)}, selected={len(admin_selected)}")
    except Exception as e:
        print("load_state error:", e)


def state_saver_loop() -> None:
    """Фоновый поток: сохраняет состояние каждые 30 секунд."""
    while True:
        try:
            save_state()
        except Exception as e:
            print("state_saver_loop error:", e)
        time.sleep(30)


# ===== НАСТРОЙКИ =====

def load_settings() -> dict:
    s = load_json(SETTINGS_FILE, {})
    if not isinstance(s, dict):
        s = {}
    s.setdefault("reminder_interval_min", 30)
    s.setdefault("reminders_enabled", True)
    s.setdefault(
        "reminder_text",
        "Напомню!!!\n"
        "Я бот для поиска аккаунтов, напиши команду /accfree "
        "что бы получить доступ к 2к+ акков",
    )
    return s


def save_settings(s: dict) -> None:
    save_json(SETTINGS_FILE, s)


settings = load_settings()


# ===== ЧАТЫ / ПОЛЬЗОВАТЕЛИ =====

def load_chats() -> set[int]:
    data = load_json(CHATS_FILE, [])
    if isinstance(data, list):
        return {int(x) for x in data}
    return set()


def save_chats(chats: set[int]) -> None:
    save_json(CHATS_FILE, sorted(chats))


known_chats: set[int] = load_chats()


def load_chat_users() -> dict:
    data = load_json(USERS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_chat_users(data: dict) -> None:
    save_json(USERS_FILE, data)


def remember_user(chat_id: int, user) -> None:
    if not user or chat_id > 0:
        return
    uid = getattr(user, "id", None)
    if not uid or getattr(user, "is_bot", False):
        return
    name = (
        getattr(user, "first_name", None)
        or getattr(user, "username", None)
        or str(uid)
    )
    data = load_chat_users()
    key = str(chat_id)
    lst = data.get(key) or []
    ids = {int(x.get("id")) for x in lst if isinstance(x, dict) and x.get("id") is not None}
    if int(uid) in ids:
        for x in lst:
            if int(x.get("id")) == int(uid):
                x["name"] = str(name)[:64]
                break
    else:
        lst.append({"id": int(uid), "name": str(name)[:64]})
    if len(lst) > 500:
        lst = lst[-500:]
    data[key] = lst
    save_chat_users(data)


def get_chat_user_list(chat_id: int) -> list:
    data = load_chat_users()
    lst = data.get(str(chat_id)) or []
    return [x for x in lst if isinstance(x, dict) and x.get("id")]


def format_mentions(users: list, text: str) -> str:
    parts = []
    for u in users:
        uid = int(u["id"])
        name = esc(str(u.get("name") or uid))
        parts.append(f'<a href="tg://user?id={uid}">{name}</a>')
    mentions = "  ".join(parts)
    body = (text or " ").strip() or "👋 "
    return f"{body}\n\n{mentions}"


def send_random_mentions(chat_id: int, count, text: str) -> tuple[bool, str]:
    import random as _rnd
    users = get_chat_user_list(chat_id)
    if not users:
        return False, "Нет известных участников (пусть напишут /accfree в чате)"
    if count is None or count <= 0 or count >= len(users):
        chosen = users
    else:
        chosen = _rnd.sample(users, count)
    msg = format_mentions(chosen, text)
    try:
        bot.send_message(chat_id, msg, parse_mode="HTML", disable_web_page_preview=True)
        return True, f"отмечено {len(chosen)}"
    except Exception as e:
        return False, str(e)


def remember_chat(chat_id: int, chat_type: str | None = None) -> None:
    if chat_id > 0:
        return
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


# ===== АДМИН =====

def is_admin(uid: int) -> bool:
    if uid in _ADMIN_IDS:
        return True
    if uid in admin_sessions:
        return True
    return False


def secret_ok(text: str) -> bool:
    if not _ADMIN_SECRET:
        print("WARN: ADMIN_SECRET empty in Env")
        return False
    got = (text or "").strip()
    exp = _ADMIN_SECRET.strip()
    if not got:
        return False
    a = hashlib.sha256(got.encode("utf-8")).digest()
    b = hashlib.sha256(exp.encode("utf-8")).digest()
    return py_secrets.compare_digest(a, b)


def get_chat_title(cid: int) -> str:
    if cid in chat_title_cache:
        return chat_title_cache[cid]
    try:
        ch = bot.get_chat(cid)
        title = getattr(ch, "title", None) or getattr(ch, "username", None) or str(cid)
        chat_title_cache[cid] = str(title)[:40]
        return chat_title_cache[cid]
    except Exception:
        chat_title_cache[cid] = str(cid)
        return str(cid)


def selection_kb(uid: int) -> types.InlineKeyboardMarkup:
    chats = sorted(load_chats())
    selected = admin_selected.setdefault(uid, set())
    kb = types.InlineKeyboardMarkup()
    for cid in chats[:80]:
        mark = "✅ " if cid in selected else "⬜ "
        title = get_chat_title(cid)
        short = title if len(title) <= 28 else title[:25] + "..."
        kb.add(
            types.InlineKeyboardButton(
                f"{mark} {short}",
                callback_data=f"adm:sel:{cid}",
            )
        )
    if len(chats) > 80:
        kb.add(types.InlineKeyboardButton(f"… ещё {len(chats)-80} не показаны", callback_data="noop"))
    kb.row(
        types.InlineKeyboardButton("Все", callback_data="adm:selall"),
        types.InlineKeyboardButton("Сброс", callback_data="adm:selnone"),
    )
    kb.add(types.InlineKeyboardButton("🔄 Обновить названия", callback_data="adm:pick"))
    kb.add(types.InlineKeyboardButton("« Назад", callback_data="adm:panel"))
    return kb


def selection_text(uid: int) -> str:
    chats = load_chats()
    selected = admin_selected.get(uid) or set()
    return (
        f"🎯 <b>Выбор чатов</b>\n"
        f"Всего: <b>{len(chats)}</b> · Выбрано: <b>{len(selected)}</b>\n\n"
        f"Отметь чаты, затем рассылка или напоминание."
    )


def ctx_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def esc(s: str) -> str:
    return str(s or " ").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def show_menu(chat_id, user_id, query, reply_to=None, chat_type=None):
    if chat_type in ("group", "supergroup") or (chat_type is None and chat_id < 0):
        remember_chat(chat_id, chat_type or "supergroup")
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


def admin_kb() -> types.InlineKeyboardMarkup:
    s = load_settings()
    mins = int(s.get("reminder_interval_min") or 30)
    on = "вкл" if s.get("reminders_enabled", True) else "выкл"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🎯 Выбрать чаты", callback_data="adm:pick"))
    kb.add(types.InlineKeyboardButton("📣 Рассылка в выбранные", callback_data="adm:broadcast"))
    kb.add(types.InlineKeyboardButton(f"⏱ КД авто: {mins} мин", callback_data="adm:interval"))
    kb.add(types.InlineKeyboardButton("✏️ Текст авто-сообщений", callback_data="adm:text"))
    kb.add(types.InlineKeyboardButton(f"🔔 Авто: {on}", callback_data="adm:toggle"))
    kb.add(types.InlineKeyboardButton("📤 Авто-текст в выбранные", callback_data="adm:test"))
    kb.add(types.InlineKeyboardButton("👥 Отметить людей", callback_data="adm:mention"))
    kb.add(types.InlineKeyboardButton("🚪 Выйти", callback_data="adm:logout"))
    return kb


def admin_panel_text() -> str:
    s = load_settings()
    chats = load_chats()
    return (
        "🛠 <b>Админ-панель</b>\n\n"
        f"Чатов в базе: <b>{len(chats)}</b>\n"
        f"Авто-сообщения: <b>{'вкл' if s.get('reminders_enabled', True) else 'выкл'}</b>\n"
        f"Интервал: <b>{int(s.get('reminder_interval_min') or 30)}</b> мин\n\n"
        "1) Выбери чаты\n"
        "2) Рассылка или авто-текст\n"
        "Если чата нет в списке — напиши /accfree в той группе."
    )


def send_reminders(manual: bool = False, only: set[int] | None = None) -> tuple[int, int]:
    s = load_settings()
    if not manual and not s.get("reminders_enabled", True):
        print("reminders disabled, skip")
        return 0, 0
    text = s.get("reminder_text") or "Напомню!!! /accfree "
    chats = list(only) if only is not None else list(load_chats())
    ok = 0
    fail = 0
    for cid in chats:
        try:
            bot.send_message(cid, text)
            ok += 1
            time.sleep(0.05)
        except Exception as e:
            fail += 1
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
    return ok, fail


def reminder_loop() -> None:
    while True:
        s = load_settings()
        mins = max(1, int(s.get("reminder_interval_min") or 30))
        time.sleep(mins * 60)
        try:
            send_reminders(manual=False)
        except Exception as e:
            print("reminder_loop:", e)


def broadcast(text: str, only: set[int] | None = None) -> tuple[int, int]:
    chats = list(only) if only is not None else list(load_chats())
    ok = fail = 0
    for cid in chats:
        try:
            bot.send_message(cid, text, parse_mode="HTML")
            ok += 1
            time.sleep(0.05)
        except Exception as e:
            fail += 1
            err = str(e).lower()
            if any(x in err for x in ("blocked", "kicked", "forbidden", "not found")):
                forget_chat(cid)
    return ok, fail


# ===== КОМАНДЫ =====

@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        remember_chat(message.chat.id, message.chat.type)
        remember_user(message.chat.id, message.from_user)
    bot.reply_to(
        message,
        "Список общих Steam-аккаунтов.\n\n"
        "<b>/accfree</b> - все аккаунты\n"
        "<b>/accfree forza</b> - поиск по игре\n\n"
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
    if message.chat.type in ("group", "supergroup"):
        remember_user(message.chat.id, message.from_user)
    raw = message.text or " "
    parts = raw.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    show_menu(
        message.chat.id,
        uid,
        query or None,
        reply_to=message,
        chat_type=message.chat.type,
    )


@bot.message_handler(commands=["auth"])
def cmd_auth(message: types.Message):
    if message.chat.type != "private":
        bot.reply_to(message, "Только в личке с ботом.")
        return
    uid = message.from_user.id
    parts = (message.text or " ").split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Напиши: <code>/auth секрет</code>")
        return
    if not _ADMIN_SECRET:
        bot.reply_to(
            message,
            "На сервере пустой <b>ADMIN_SECRET</b>. Добавь в Render → Environment.",
        )
        return
    if secret_ok(parts[1]):
        admin_sessions.add(uid)
        save_state()  # <-- сохраняем сразу
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        bot.send_message(
            message.chat.id,
            "✅ Доступ открыт.\nДальше: /admin",
        )
    else:
        bot.reply_to(message, "Нет доступа.")


@bot.message_handler(commands=["admin", "panel"])
def cmd_admin(message: types.Message):
    if message.chat.type != "private":
        bot.reply_to(message, "Админка только в личке с ботом.")
        return
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(
            message,
            "Нет доступа.\nВ личке: <code>/auth ваш_секрет</code>",
        )
        return
    bot.reply_to(message, admin_panel_text(), reply_markup=admin_kb())


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message: types.Message):
    admin_wait.pop(message.from_user.id, None)
    bot.reply_to(message, "Ок, отменено.")


@bot.my_chat_member_handler()
def on_my_chat_member(message: types.ChatMemberUpdated):
    try:
        chat = message.chat
        new = message.new_chat_member
        old = message.old_chat_member
        status = (new.status if new else "") or ""
        old_status = (old.status if old else "") or ""
        if status in ("member", "administrator"):
            if chat.type in ("group", "supergroup"):
                remember_chat(chat.id, chat.type)
                if old_status in ("left", "kicked", ""):
                    try:
                        bot.send_message(
                            chat.id,
                            "Ура! меня добавили в чат\n"
                            "Напиши команду /accfree для доступа к базе акков",
                        )
                    except Exception as e:
                        print("welcome msg:", e)
        elif status in ("left", "kicked"):
            forget_chat(chat.id)
    except Exception as e:
        print("my_chat_member:", e)


@bot.inline_handler(lambda query: True)
def on_inline_query(query: types.InlineQuery):
    qtext = (query.query or "").strip()
    uid = query.from_user.id if query.from_user else 0
    results = []
    found = search_accounts(qtext if len(qtext) >= 2 else None)
    if len(qtext) < 2:
        found = found[:20]
    else:
        found = found[:30]
    for i, a in enumerate(found):
        login = a.get("l") or "?"
        password = a.get("p") or "?"
        games = a.get("g") or "—"
        title = (games[:60] + "…") if len(games) > 60 else games
        desc = f"{login}"
        body = (
            f"🎮 <b>{esc(games)}</b>\n\n"
            f"👤 Логин:\n<code>{esc(login)}</code>\n\n"
            f"🔑 Пароль:\n<code>{esc(password)}</code>"
        )
        rid = f"{i}_{hash(login) % 10_000_000}"
        results.append(
            types.InlineQueryResultArticle(
                id=rid[:60],
                title=title or f"Аккаунт {i+1}",
                description=desc,
                input_message_content=types.InputTextMessageContent(
                    body,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                ),
            )
        )
    if not results:
        results.append(
            types.InlineQueryResultArticle(
                id="empty",
                title="Ничего не найдено",
                description="Попробуй другое название игры",
                input_message_content=types.InputTextMessageContent(
                    f"По запросу «{esc(qtext)}» ничего не найдено.",
                    parse_mode="HTML",
                ),
            )
        )
    try:
        bot.answer_inline_query(
            query.id,
            results,
            cache_time=5,
            is_personal=True,
        )
    except Exception as e:
        print("inline error:", e)


@bot.callback_query_handler(func=lambda c: True)
def on_callback(cq: types.CallbackQuery):
    data = cq.data or ""
    uid = cq.from_user.id
    chat_id = cq.message.chat.id if cq.message else 0

    # ---- admin callbacks ----
    if data.startswith("adm:"):
        if not is_admin(uid):
            bot.answer_callback_query(cq.id, "Нет доступа", show_alert=True)
            return
        action = data.split(":", 1)[1]

        if action == "logout":
            admin_sessions.discard(uid)
            admin_wait.pop(uid, None)
            admin_selected.pop(uid, None)
            save_state()  # <-- сохраняем сразу
            bot.answer_callback_query(cq.id, "Вышел")
            try:
                bot.edit_message_text("Сессия админа закрыта.", chat_id, cq.message.message_id)
            except Exception:
                bot.send_message(chat_id, "Сессия админа закрыта.")
            return

        if action == "panel":
            admin_wait.pop(uid, None)
            bot.answer_callback_query(cq.id)
            try:
                bot.edit_message_text(
                    admin_panel_text(), chat_id, cq.message.message_id, reply_markup=admin_kb()
                )
            except Exception:
                bot.send_message(chat_id, admin_panel_text(), reply_markup=admin_kb())
            return

        if action == "pick":
            bot.answer_callback_query(cq.id)
            admin_selected.setdefault(uid, set())
            try:
                bot.edit_message_text(
                    selection_text(uid),
                    chat_id,
                    cq.message.message_id,
                    reply_markup=selection_kb(uid),
                )
            except Exception:
                bot.send_message(chat_id, selection_text(uid), reply_markup=selection_kb(uid))
            return

        if action == "selall":
            admin_selected[uid] = set(load_chats())
            bot.answer_callback_query(cq.id, "Все")
            try:
                bot.edit_message_text(
                    selection_text(uid), chat_id, cq.message.message_id, reply_markup=selection_kb(uid)
                )
            except Exception:
                pass
            return

        if action == "selnone":
            admin_selected[uid] = set()
            bot.answer_callback_query(cq.id, "Сброс")
            try:
                bot.edit_message_text(
                    selection_text(uid), chat_id, cq.message.message_id, reply_markup=selection_kb(uid)
                )
            except Exception:
                pass
            return

        if action.startswith("sel:"):
            try:
                cid = int(action.split(":")[1])
            except Exception:
                bot.answer_callback_query(cq.id)
                return
            sel = admin_selected.setdefault(uid, set())
            if cid in sel:
                sel.discard(cid)
            else:
                sel.add(cid)
            bot.answer_callback_query(cq.id)
            try:
                bot.edit_message_text(
                    selection_text(uid), chat_id, cq.message.message_id, reply_markup=selection_kb(uid)
                )
            except Exception:
                pass
            return

        if action == "toggle":
            s = load_settings()
            s["reminders_enabled"] = not s.get("reminders_enabled", True)
            save_settings(s)
            bot.answer_callback_query(cq.id, "Сохранено")
            try:
                bot.edit_message_text(
                    admin_panel_text(), chat_id, cq.message.message_id, reply_markup=admin_kb()
                )
            except Exception:
                pass
            return

        if action == "interval":
            admin_wait[uid] = "interval"
            bot.answer_callback_query(cq.id)
            bot.send_message(
                chat_id,
                "Введи интервал автосообщений в <b>минутах</b> (число от 1 до 1440).\n"
                "Отмена: /cancel",
            )
            return

        if action == "text":
            admin_wait[uid] = "text"
            bot.answer_callback_query(cq.id)
            bot.send_message(
                chat_id,
                "Пришли <b>новый текст</b> напоминания одним сообщением.\nОтмена: /cancel",
            )
            return

        if action == "mention":
            sel = admin_selected.get(uid) or set()
            if not sel:
                bot.answer_callback_query(
                    cq.id, "Сначала выбери чаты", show_alert=True
                )
                return
            admin_wait[uid] = "mention_count"
            bot.answer_callback_query(cq.id)
            bot.send_message(
                chat_id,
                f"👥 Отметка в <b>{len(sel)}</b> чатах.\n\n"
                f"Сколько человек отметить?\n"
                f"• число (например <code>5</code>) — случайные\n"
                f"• <code>all</code> — все известные боту\n\n"
                f"Бот знает только тех, кто писал /accfree в группе.\n"
                f"Отмена: /cancel",
            )
            return

        if action == "broadcast":
            sel = admin_selected.get(uid) or set()
            if not sel:
                bot.answer_callback_query(
                    cq.id, "Сначала выбери чаты (🎯 Выбрать чаты)", show_alert=True
                )
                return
            admin_wait[uid] = "broadcast"
            bot.answer_callback_query(cq.id)
            bot.send_message(
                chat_id,
                f"📣 Текст рассылки в <b>{len(sel)}</b> выбранных чатов.\n"
                f"Пришли сообщение. Отмена: /cancel",
            )
            return

        if action == "test":
            sel = admin_selected.get(uid) or set()
            if not sel:
                bot.answer_callback_query(
                    cq.id, "Сначала выбери чаты (🎯)", show_alert=True
                )
                return
            bot.answer_callback_query(cq.id, "Шлю…")
            ok, fail = send_reminders(manual=True, only=sel)
            bot.send_message(chat_id, f"Готово. OK: <b>{ok}</b>, ошибок: <b>{fail}</b>")
            return

        bot.answer_callback_query(cq.id)
        return

    # КД только на выбор аккаунта
    if data.startswith("a:"):
        now = time.time()
        prev = last_click.get(uid, 0)
        left = CLICK_COOLDOWN - (now - prev)
        if left > 0:
            bot.answer_callback_query(
                cq.id, f"Подожди {int(left) + 1} сек.", show_alert=False
            )
            return
        last_click[uid] = now

    if cq.message and cq.message.chat.type in ("group", "supergroup"):
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
        bot.send_message(chat_id, fmt_acc(found[idx], idx + 1))
        bot.answer_callback_query(cq.id)
        return

    bot.answer_callback_query(cq.id)


@bot.message_handler(
    func=lambda m: bool(m.text)
    and not (m.text or "").startswith("/")
    and m.chat.type == "private"
)
def on_private_text(message: types.Message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    # ввод секрета
    if not is_admin(uid) and secret_ok(text):
        admin_sessions.add(uid)
        save_state()
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        bot.send_message(
            message.chat.id,
            "✅ Доступ открыт.\nКоманда: /admin",
            reply_markup=admin_kb(),
        )
        return

    # шаги админки
    if is_admin(uid) and uid in admin_wait:
        step = admin_wait.pop(uid)

        if step == "mention_count":
            raw = text.strip().lower()
            if raw in ("all", "все", "0", "-1"):
                count = None
            else:
                try:
                    count = int(raw)
                    if count < 1:
                        raise ValueError()
                except Exception:
                    bot.reply_to(message, "Нужно число >= 1 или <code>all</code>")
                    admin_wait[uid] = "mention_count"
                    return
            admin_selected.setdefault(uid, set())
            admin_wait[uid] = f"mention_text:{count if count is not None else 'all'}"
            bot.reply_to(
                message,
                "Теперь пришли <b>текст</b> перед отметками.\n"
                "Пример: <code>Внимание розыгрыш!</code>",
            )
            return

        if step.startswith("mention_text:"):
            mode = step.split(":", 1)[1]
            count = None if mode == "all" else int(mode)
            sel = admin_selected.get(uid) or set()
            if not sel:
                bot.reply_to(message, "Нет выбранных чатов.")
                return
            bot.reply_to(message, f"Отправляю отметки в {len(sel)} чатов…")
            ok = fail = 0
            details = []
            for cid in list(sel):
                good, info = send_random_mentions(cid, count, text)
                if good:
                    ok += 1
                else:
                    fail += 1
                details.append(f"{cid}: {info}")
            bot.send_message(
                message.chat.id,
                f"Готово. OK: <b>{ok}</b>, ошибок: <b>{fail}</b>\n"
                + "\n".join(details[:15]),
            )
            return

        if step == "broadcast":
            sel = admin_selected.get(uid) or set()
            if not sel:
                bot.reply_to(message, "Нет выбранных чатов. Открой 🎯 Выбрать чаты.")
                return
            bot.reply_to(message, f"Рассылаю в {len(sel)} чатов…")
            ok, fail = broadcast(text, only=sel)
            bot.send_message(message.chat.id, f"Готово. OK: <b>{ok}</b>, ошибок: <b>{fail}</b>")
            return

        if step == "interval":
            try:
                mins = int(text.strip())
                if mins < 1 or mins > 1440:
                    raise ValueError()
            except Exception:
                bot.reply_to(message, "Нужно число от 1 до 1440.")
                return
            s = load_settings()
            s["reminder_interval_min"] = mins
            save_settings(s)
            bot.reply_to(message, f"Интервал: <b>{mins}</b> мин. Сработает после следующего цикла.")
            return

        if step == "text":
            s = load_settings()
            s["reminder_text"] = text
            save_settings(s)
            bot.reply_to(message, "Текст напоминания сохранён.")
            return

    # обычный текст в личке
    if not is_admin(uid):
        bot.reply_to(message, "Команды: /accfree — список аккаунтов.")


# ===== Flask routes =====

@app.get("/")
def health():
    return {
        "ok": True,
        "accounts": len(ACCOUNTS),
        "chats": len(known_chats),
        "webhook": bool(WEBHOOK_URL),
        "admin_secret_set": bool(_ADMIN_SECRET),
        "admin_sessions": len(admin_sessions),
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


# ===== Запуск =====

# 1. Восстанавливаем состояние ИЗ ФАЙЛА при старте
load_state()

setup_webhook()

# 2. Фоновый поток автосохранения (каждые 30 сек)
_t_state = threading.Thread(target=state_saver_loop, name="state_saver", daemon=True)
_t_state.start()

# 3. Фоновый поток напоминаний
_t = threading.Thread(target=reminder_loop, name="reminder", daemon=True)
_t.start()

# 4. Сохраняем состояние при штатном завершении
import atexit
atexit.register(save_state)

print(
    f"Ready | accounts={len(ACCOUNTS)} | chats={len(known_chats)} | "
    f"admin_secret={'yes' if _ADMIN_SECRET else 'NO'} | "
    f"admin_sessions={len(admin_sessions)}"
)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
