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
# Постоянные данные: на Render укажи DATA_DIR=/data и Persistent Disk,
# иначе при рестарте баны/сессии/чаты пропадут (эфемерный диск).
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT))).resolve()
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    print("DATA_DIR mkdir:", e)
    DATA_DIR = ROOT

CHATS_FILE = DATA_DIR / "known_chats.json"
USERS_FILE = DATA_DIR / "chat_users.json"
USERS_INDEX_FILE = DATA_DIR / "users_index.json"
SETTINGS_FILE = DATA_DIR / "bot_settings.json"
SESSIONS_FILE = DATA_DIR / "admin_sessions.json"
BANS_FILE = DATA_DIR / "banned_users.json"
print(f"DATA_DIR={DATA_DIR}")
PAGE_SIZE = 10
CLICK_COOLDOWN = 5

# Секрет ТОЛЬКО из окружения — в Git его нет.
# На Render: ADMIN_SECRET = твоё слово
_ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
# Постоянные админы по Telegram user id: "123,456"
_ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not set")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
# Обход лаунчеров: ссылка (рекомендуется для ~400MB) или file_id / локальный путь <50MB
BYPASS_DOWNLOAD_URL = os.getenv("BYPASS_DOWNLOAD_URL", "").strip()
BYPASS_FILE_ID = os.getenv("BYPASS_FILE_ID", "").strip()
BYPASS_ZIP_PATH = os.getenv("BYPASS_ZIP_PATH", "").strip()
PORT = int(os.getenv("PORT", "10000"))

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

user_results: dict[str, dict] = {}
last_click: dict[int, float] = {}
last_cmd: dict[int, float] = {}
# сессии админов после ввода секрета (до рестарта)
admin_sessions: set[int] = set()
# ожидание текста рассылки / настройки
admin_wait: dict[int, str] = {}
# uid -> set of chat_ids selected for action
admin_selected: dict[int, set[int]] = {}
chat_title_cache: dict[int, str] = {}


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


def load_admin_sessions() -> set[int]:
    data = load_json(SESSIONS_FILE, [])
    out: set[int] = set()
    if isinstance(data, list):
        for x in data:
            try:
                out.add(int(x))
            except Exception:
                pass
    return out


def save_admin_sessions() -> None:
    save_json(SESSIONS_FILE, sorted(admin_sessions))


admin_sessions.clear()
admin_sessions.update(load_admin_sessions())
print("admin sessions:", len(admin_sessions), "secret_set:", bool(_ADMIN_SECRET))
print("bans loaded:", len(load_json(BANS_FILE, {})))
if not _ADMIN_IDS:
    print("WARN: set ADMIN_IDS in Env so admin access survives restarts")


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


def index_user(user) -> None:
    """Глобальный индекс username -> id (для банов)."""
    if not user:
        return
    uid = getattr(user, "id", None)
    if not uid or getattr(user, "is_bot", False):
        return
    uname = (getattr(user, "username", None) or "").strip()
    name = getattr(user, "first_name", None) or uname or str(uid)
    data = load_json(USERS_INDEX_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data[str(uid)] = {
        "id": int(uid),
        "username": uname,
        "name": str(name)[:64],
    }
    if uname:
        data["@" + uname.lower()] = data[str(uid)]
        data[uname.lower()] = data[str(uid)]
    save_json(USERS_INDEX_FILE, data)


def find_user_in_index(query: str) -> tuple[int | None, str]:
    """Найти id по username в индексе бота."""
    raw = (query or "").strip().lstrip("@").lower()
    if not raw:
        return None, ""
    data = load_json(USERS_INDEX_FILE, {})
    if not isinstance(data, dict):
        return None, ""
    if raw.isdigit() and raw in data:
        e = data[raw]
        return int(e.get("id") or raw), e.get("username") or ""
    for key in (raw, "@" + raw):
        e = data.get(key)
        if isinstance(e, dict) and e.get("id"):
            return int(e["id"]), str(e.get("username") or raw)
    # linear scan
    for k, e in data.items():
        if not isinstance(e, dict):
            continue
        if str(e.get("username") or "").lower() == raw:
            return int(e["id"]), str(e.get("username") or raw)
    return None, ""


def remember_user(chat_id: int, user) -> None:
    """Запомнить участника (индекс + список чата для отметок)."""
    index_user(user)
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
    # dedupe by id
    ids = {int(x.get("id")) for x in lst if isinstance(x, dict) and x.get("id") is not None}
    if int(uid) in ids:
        # update name
        for x in lst:
            if int(x.get("id")) == int(uid):
                x["name"] = str(name)[:64]
                break
    else:
        lst.append({"id": int(uid), "name": str(name)[:64]})
        # cap per chat
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
    mentions = " ".join(parts)
    body = (text or "").strip() or "👋"
    return f"{body}\n\n{mentions}"


def send_random_mentions(chat_id: int, count: int | None, text: str) -> tuple[bool, str]:
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
    # только группы / супергруппы (отрицательные id)
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


def is_admin(uid: int) -> bool:
    if uid in _ADMIN_IDS:
        return True
    if uid in admin_sessions:
        return True
    # gunicorn workers: подтянуть с диска
    if uid in load_admin_sessions():
        admin_sessions.add(uid)
        return True
    return False



def load_bans() -> dict:
    data = load_json(BANS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_bans(data: dict) -> None:
    save_json(BANS_FILE, data)


def is_banned(uid: int) -> dict | None:
    """Вернуть запись бана или None. Просроченные баны снимаются."""
    data = load_bans()
    b = data.get(str(uid))
    if not isinstance(b, dict):
        return None
    until = b.get("until")  # unix ts or None = forever
    if until is not None:
        try:
            if time.time() >= float(until):
                del data[str(uid)]
                save_bans(data)
                return None
        except Exception:
            pass
    if b.get("reason") is not None:
        return b
    return None


def ban_user(
    uid: int,
    reason: str,
    by: str = "",
    duration: str = "forever",
    username: str = "",
) -> None:
    from datetime import datetime, timezone, timedelta
    msk = timezone(timedelta(hours=3))
    now = datetime.now(msk)
    until_ts = None
    until_label = "навсегда"
    if duration == "hour":
        until_ts = time.time() + 3600
        until_label = "1 час"
    elif duration == "day":
        until_ts = time.time() + 86400
        until_label = "1 день"
    elif duration == "year":
        until_ts = time.time() + 365 * 86400
        until_label = "1 год"
    data = load_bans()
    data[str(uid)] = {
        "reason": (reason or "без указания причины").strip()[:500],
        "at": now.strftime("%d.%m.%Y %H:%M"),
        "by": by or "",
        "until": until_ts,
        "until_label": until_label,
        "username": (username or "").lstrip("@"),
    }
    save_bans(data)


def unban_user(uid: int) -> bool:
    data = load_bans()
    if str(uid) in data:
        del data[str(uid)]
        save_bans(data)
        return True
    return False


def resolve_username(text: str) -> tuple[int | None, str, str]:
    """@user или user -> (id, username, error).
    Сначала локальный индекс (кто писал боту), потом Telegram API.
    """
    raw = (text or "").strip().lstrip("@")
    if not raw:
        return None, "", "Пустой username"
    if raw.isdigit():
        return int(raw), "", ""
    # 1) наша база
    uid, uname = find_user_in_index(raw)
    if uid is not None:
        return uid, uname or raw, ""
    # 2) Telegram (часто не работает, пока юзер не писал боту)
    try:
        ch = bot.get_chat("@" + raw)
        uid = getattr(ch, "id", None)
        uname = getattr(ch, "username", None) or raw
        if uid is None:
            return None, raw, "Не удалось получить id"
        index_user(ch)  # type: ignore
        return int(uid), str(uname), ""
    except Exception:
        return (
            None,
            raw,
            f"Не найден @{raw}.\n\n"
            "Telegram не отдаёт id, пока человек <b>сам не напишет боту</b> "
            "(/start или /accfree).\n"
            "Пусть напишет боту в личку, потом повтори бан — "
            "или пришли числовой id (@userinfobot).",
        )


def ban_reply_text(info: dict) -> str:
    reason = esc(str(info.get("reason") or "не указана"))
    at = esc(str(info.get("at") or ""))
    label = esc(str(info.get("until_label") or "навсегда"))
    until = info.get("until")
    until_line = ""
    if until is not None:
        try:
            from datetime import datetime, timezone, timedelta
            msk = timezone(timedelta(hours=3))
            until_line = "\n⏳ До: " + datetime.fromtimestamp(float(until), msk).strftime("%d.%m.%Y %H:%M")
        except Exception:
            until_line = ""
    extra = f"\n📅 {at}" if at else ""
    return (
        "🚫 <b>Вы заблокированы в боте</b>\n\n"
        f"Причина: {reason}\n"
        f"Срок: <b>{label}</b>{until_line}{extra}"
    )


def reject_if_banned(message_or_uid, reply_func=None) -> bool:
    """True = пользователь забанен, ответ уже отправлен."""
    if hasattr(message_or_uid, "from_user"):
        uid = message_or_uid.from_user.id
        msg = message_or_uid
    else:
        uid = int(message_or_uid)
        msg = None
    if is_admin(uid):
        return False
    info = is_banned(uid)
    if not info:
        return False
    text = ban_reply_text(info)
    if msg is not None:
        try:
            bot.reply_to(msg, text)
        except Exception:
            bot.send_message(msg.chat.id, text)
    return True


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
        mark = "✅" if cid in selected else "⬜"
        title = get_chat_title(cid)
        short = title if len(title) <= 28 else title[:25] + "..."
        # callback limit 64 bytes; cid fits
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
            f"Поиск: <code>/accfree название игры</code>\n\n"
            f"Чтобы обойти лаунчеры (Rockstar, Ubisoft, EA и т.д.)\n"
            f"напиши команду /обход"
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
    sel_n = 0  # подсказка в тексте панели
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🎯 Выбрать чаты", callback_data="adm:pick"))
    kb.add(types.InlineKeyboardButton("📣 Рассылка в выбранные", callback_data="adm:broadcast"))
    kb.add(types.InlineKeyboardButton(f"⏱ КД авто: {mins} мин", callback_data="adm:interval"))
    kb.add(types.InlineKeyboardButton("✏️ Текст авто-сообщений", callback_data="adm:text"))
    kb.add(types.InlineKeyboardButton(f"🔔 Авто: {on}", callback_data="adm:toggle"))
    kb.add(types.InlineKeyboardButton("📤 Авто-текст в выбранные", callback_data="adm:test"))
    kb.add(types.InlineKeyboardButton("👥 Отметить людей", callback_data="adm:mention"))
    kb.add(types.InlineKeyboardButton("🚫 Забанить", callback_data="adm:ban"))
    kb.add(types.InlineKeyboardButton("✅ Разбанить", callback_data="adm:unban"))
    kb.add(types.InlineKeyboardButton("📋 Список банов", callback_data="adm:banlist"))
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
        "Если чата нет в списке — напиши /accfree в той группе.\n\n"
        "⚠️ Без Persistent Disk на Render баны сбрасываются после рестарта."
    )


def send_reminders(manual: bool = False, only: set[int] | None = None) -> tuple[int, int]:
    s = load_settings()
    if not manual and not s.get("reminders_enabled", True):
        print("reminders disabled, skip")
        return 0, 0
    text = s.get("reminder_text") or "Напомню!!! /accfree"
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


@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    if reject_if_banned(message):
        return
    index_user(message.from_user)
    if message.chat.type in ("group", "supergroup"):
        remember_chat(message.chat.id, message.chat.type)
        remember_user(message.chat.id, message.from_user)
    bot.reply_to(
        message,
        "Список общих Steam-аккаунтов.\n\n"
        "<b>/accfree</b> - все аккаунты\n"
        "<b>/accfree forza</b> - поиск по игре\n"
        "<b>/обход</b> - обход лаунчеров (Rockstar, Ubisoft, EA…)\n\n"
        "Работает в личке и в группах.",
    )



@bot.message_handler(commands=["obhod", "обход", "bypass"])
def cmd_obhod(message: types.Message):
    """Выдать архив обхода лаунчеров (Rockstar, Ubisoft, EA и т.д.)."""
    if reject_if_banned(message):
        return
    index_user(message.from_user)
    uid = message.from_user.id if message.from_user else 0
    now = time.time()
    left = CLICK_COOLDOWN - (now - last_cmd.get(uid, 0))
    if left > 0:
        bot.reply_to(message, f"⏳ Подожди ещё <b>{int(left) + 1}</b> сек.")
        return
    last_cmd[uid] = now

    if message.chat.type in ("group", "supergroup"):
        remember_chat(message.chat.id, message.chat.type)
        remember_user(message.chat.id, message.from_user)

    caption = (
        "🛠 <b>Обход лаунчеров</b>\n"
        "Rockstar / Ubisoft / EA и др.\n\n"
        "Скачай архив и следуй инструкции внутри."
    )

    # 1) Готовый file_id (после одной ручной загрузки через @BotFather/локальный API)
    if BYPASS_FILE_ID:
        try:
            bot.send_document(
                message.chat.id,
                BYPASS_FILE_ID,
                caption=caption,
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )
            return
        except Exception as e:
            bot.reply_to(message, f"Не удалось отправить по file_id: {esc(str(e))}")
            return

    # 2) Локальный файл — только если < 49 MB (лимит Bot API)
    if BYPASS_ZIP_PATH:
        p = Path(BYPASS_ZIP_PATH)
        if p.is_file():
            size = p.stat().st_size
            if size <= 49 * 1024 * 1024:
                wait = bot.reply_to(message, "⏳ Отправляю файл…")
                try:
                    with open(p, "rb") as f:
                        bot.send_document(
                            message.chat.id,
                            f,
                            caption=caption,
                            parse_mode="HTML",
                            visible_file_name=p.name,
                        )
                    try:
                        bot.delete_message(message.chat.id, wait.message_id)
                    except Exception:
                        pass
                except Exception as e:
                    bot.reply_to(message, f"Ошибка отправки: {esc(str(e))}")
                return
            # слишком большой — только ссылка
            if BYPASS_DOWNLOAD_URL:
                bot.reply_to(
                    message,
                    caption + "\n\n"
                    f"📦 Файл большой ({size // (1024*1024)} МБ), Telegram-бот не шлёт >50 МБ.\n"
                    f"Скачай по ссылке:\n{BYPASS_DOWNLOAD_URL}",
                )
                return
            bot.reply_to(
                message,
                caption + "\n\n"
                f"Файл на сервере {size // (1024*1024)} МБ, лимит бота 50 МБ.\n"
                "Укажи в Env <code>BYPASS_DOWNLOAD_URL</code> со ссылкой на скачивание.",
            )
            return
        bot.reply_to(message, "BYPASS_ZIP_PATH указан, но файл не найден.")
        return

    # 3) Только ссылка
    if BYPASS_DOWNLOAD_URL:
        bot.reply_to(
            message,
            caption + "\n\n"
            f"📥 Скачать (~400 МБ):\n{BYPASS_DOWNLOAD_URL}",
        )
        return

    bot.reply_to(
        message,
        "🛠 Обход лаунчеров пока не настроен.\n\n"
        "Админ: в Render Env добавь один из вариантов:\n"
        "• <code>BYPASS_DOWNLOAD_URL</code> — прямая ссылка на zip (для ~400 МБ)\n"
        "• <code>BYPASS_FILE_ID</code> — если файл уже залит в Telegram\n"
        "• <code>BYPASS_ZIP_PATH</code> — путь к файлу &lt; 50 МБ",
    )


@bot.message_handler(commands=["accfree"])
def cmd_accfree(message: types.Message):
    if reject_if_banned(message):
        return
    index_user(message.from_user)
    uid = message.from_user.id
    now = time.time()
    left = CLICK_COOLDOWN - (now - last_cmd.get(uid, 0))
    if left > 0:
        bot.reply_to(message, f"⏳ Подожди ещё <b>{int(left) + 1}</b> сек.")
        return
    last_cmd[uid] = now
    if message.chat.type in ("group", "supergroup"):
        remember_user(message.chat.id, message.from_user)
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



@bot.message_handler(commands=["auth"])
def cmd_auth(message: types.Message):
    if message.chat.type != "private":
        bot.reply_to(message, "Только в личке с ботом.")
        return
    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
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
        save_admin_sessions()
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        bot.send_message(
            message.chat.id,
            "✅ Доступ открыт.\nДальше: /admin",
            reply_markup=admin_kb(),
        )
    else:
        bot.reply_to(message, "Нет доступа.")


@bot.message_handler(commands=["admin", "panel"])
def cmd_admin(message: types.Message):
    """Панель только если уже авторизован (по id или сессии)."""
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
                # только при реальном добавлении, не при смене прав
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
    """Инлайн: @bot_username forza — список аккаунтов."""
    uid = query.from_user.id if query.from_user else 0
    if query.from_user:
        index_user(query.from_user)
    if uid and is_banned(uid) and not is_admin(uid):
        try:
            bot.answer_inline_query(
                query.id,
                [
                    types.InlineQueryResultArticle(
                        id="banned",
                        title="Вы заблокированы",
                        description=(is_banned(uid) or {}).get("reason") or "",
                        input_message_content=types.InputTextMessageContent(
                            ban_reply_text(is_banned(uid) or {"reason": "?"}),
                            parse_mode="HTML",
                        ),
                    )
                ],
                cache_time=1,
                is_personal=True,
            )
        except Exception:
            pass
        return
    qtext = (query.query or "").strip()

    # лёгкий кд на инлайн (антиспам)
    now = time.time()
    prev = last_cmd.get(uid, 0)
    # не блокируем полностью — просто не чаще чем раз в 1 сек обновлять тяжело
    results = []
    found = search_accounts(qtext if len(qtext) >= 2 else None)
    # без запроса — топ/случайная выборка чтобы не слать все 2к
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
        # id must be unique string 1-64 bytes
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
    index_user(cq.from_user)
    chat_id = cq.message.chat.id if cq.message else 0

    # бан (кроме админ-кнопок для админов)
    if not data.startswith("adm:") and is_banned(uid) and not is_admin(uid):
        info = is_banned(uid) or {}
        bot.answer_callback_query(cq.id, "Вы заблокированы", show_alert=True)
        try:
            bot.send_message(chat_id, ban_reply_text(info))
        except Exception:
            pass
        return

    # ---- admin callbacks ----
    if data.startswith("adm:"):
        if not is_admin(uid):
            bot.answer_callback_query(cq.id, "Нет доступа", show_alert=True)
            return
        action = data.split(":", 1)[1]

        if action == "logout":
            admin_sessions.discard(uid)
            save_admin_sessions()
            admin_wait.pop(uid, None)
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



        if action == "ban":
            admin_wait[uid] = "ban_user"
            bot.answer_callback_query(cq.id)
            bot.send_message(
                chat_id,
                "🚫 Пришли <b>@username</b> пользователя\n"
                "(можно без @). Отмена: /cancel",
            )
            return

        if action == "unban":
            admin_wait[uid] = "unban_user"
            bot.answer_callback_query(cq.id)
            bot.send_message(
                chat_id,
                "✅ Пришли <b>@username</b> для разбана.\nОтмена: /cancel",
            )
            return

        if action.startswith("ban_dur:"):
            # ban_dur:hour:UID:reason_b64 or store reason in admin_wait
            parts = action.split(":", 2)
            # format ban_dur:{hour|day|year|forever}
            dur = parts[1] if len(parts) > 1 else "forever"
            st = admin_wait.get(uid) or ""
            if not st.startswith("ban_pending:"):
                bot.answer_callback_query(cq.id, "Сначала начни бан заново", show_alert=True)
                return
            # ban_pending:uid:username:reason
            try:
                _, tuid, uname, reason = st.split(":", 3)
                tuid_i = int(tuid)
            except Exception:
                bot.answer_callback_query(cq.id, "Ошибка данных", show_alert=True)
                admin_wait.pop(uid, None)
                return
            admin_wait.pop(uid, None)
            by = cq.from_user.username or str(uid)
            ban_user(tuid_i, reason, by=by, duration=dur, username=uname)
            bot.answer_callback_query(cq.id, "Забанен")
            bot.send_message(
                chat_id,
                f"🚫 @{esc(uname) or tuid} (<code>{tuid}</code>)\n"
                f"Причина: {esc(reason)}\n"
                f"Срок: <b>{esc({'hour':'1 час','day':'1 день','year':'1 год'}.get(dur, 'навсегда'))}</b>",
            )
            return

        if action == "banlist":
            bot.answer_callback_query(cq.id)
            bans = load_bans()
            if not bans:
                bot.send_message(chat_id, "Список банов пуст.")
                return
            lines = []
            for i, (k, v) in enumerate(list(bans.items())[:40]):
                if not isinstance(v, dict):
                    continue
                un = v.get("username") or ""
                un_s = f"@{esc(un)} " if un else ""
                lab = esc(v.get("until_label") or "навсегда")
                lines.append(
                    f"{un_s}<code>{esc(k)}</code> — {esc(v.get('reason') or '?')} "
                    f"· {lab} · {esc(v.get('at') or '')}"
                )
            more = f"\n… ещё {len(bans) - 40}" if len(bans) > 40 else ""
            bot.send_message(
                chat_id,
                f"📋 Банов: <b>{len(bans)}</b>\n\n" + "\n".join(lines) + more,
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

    # КД только на выбор аккаунта, листать страницы можно без ожидания
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


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message: types.Message):
    admin_wait.pop(message.from_user.id, None)
    bot.reply_to(message, "Ок, отменено.")


@bot.message_handler(
    func=lambda m: bool(m.text)
    and not (m.text or "").startswith("/")
    and m.chat.type == "private"
)
def on_private_text(message: types.Message):
    index_user(message.from_user)
    uid = message.from_user.id
    text = (message.text or "").strip()

    # 1) ввод секрета — только в личке, без упоминаний в help
    if not is_admin(uid) and secret_ok(text):
        admin_sessions.add(uid)
        save_admin_sessions()
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

    # 2) шаги админки
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
            # store count temporarily
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

        if step == "ban_user":
            target, uname, err = resolve_username(text)
            if err or target is None:
                bot.reply_to(message, f"❌ {esc(err or 'Ошибка')}")
                admin_wait[uid] = "ban_user"
                return
            if is_admin(target):
                bot.reply_to(message, "Нельзя забанить админа.")
                return
            admin_wait[uid] = f"ban_reason:{target}:{uname}"
            bot.reply_to(
                message,
                f"Пользователь @{esc(uname)} (<code>{target}</code>).\n"
                f"Теперь пришли <b>причину</b> бана.",
            )
            return
        if step.startswith("ban_reason:"):
            parts = step.split(":", 2)
            target = int(parts[1])
            uname = parts[2] if len(parts) > 2 else ""
            reason = text.strip() or "без указания причины"
            # reason may contain ":" — already full text
            admin_wait[uid] = f"ban_pending:{target}:{uname}:{reason}"
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("⏱ 1 час", callback_data="adm:ban_dur:hour"))
            kb.add(types.InlineKeyboardButton("📅 1 день", callback_data="adm:ban_dur:day"))
            kb.add(types.InlineKeyboardButton("📆 1 год", callback_data="adm:ban_dur:year"))
            kb.add(types.InlineKeyboardButton("🔒 Навсегда", callback_data="adm:ban_dur:forever"))
            bot.reply_to(
                message,
                f"Причина: {esc(reason)}\n\nВыбери <b>срок</b> бана:",
                reply_markup=kb,
            )
            return
        if step == "unban_user":
            target, uname, err = resolve_username(text)
            if err or target is None:
                bot.reply_to(message, f"❌ {esc(err or 'Ошибка')}")
                admin_wait[uid] = "unban_user"
                return
            if unban_user(target):
                bot.reply_to(
                    message,
                    f"✅ Разбанен @{esc(uname)} (<code>{target}</code>)",
                )
            else:
                bot.reply_to(message, "Этого пользователя нет в бане.")
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

    # обычный текст в личке — подсказка
    if not is_admin(uid):
        bot.reply_to(message, "Команды: /accfree — список аккаунтов.")





@app.get("/")
def health():
    return {
        "ok": True,
        "accounts": len(ACCOUNTS),
        "chats": len(known_chats),
        "webhook": bool(WEBHOOK_URL),
        "admin_secret_set": bool(_ADMIN_SECRET),
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
_t = threading.Thread(target=reminder_loop, name="reminder", daemon=True)
_t.start()
print(
    f"Ready | accounts={len(ACCOUNTS)} | chats={len(known_chats)} | "
    f"admin_secret={'yes' if _ADMIN_SECRET else 'NO'}"
)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
