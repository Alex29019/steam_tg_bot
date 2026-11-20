"""
Steam accounts list bot for groups.
/accfree [игра]
Админ: секрет из Env ADMIN_SECRET (не светить в чате и не коммитить).
Start: gunicorn -b 0.0.0.0:$PORT bot:app
Env: BOT_TOKEN, WEBHOOK_URL, ADMIN_SECRET, optional ADMIN_IDS=1,2,3
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import secrets as py_secrets
import threading
import time
from pathlib import Path

import telebot
from flask import Flask, request, jsonify, send_from_directory
from telebot import types



# Steam account check REMOVED
def check_steam(*a, **k):
    return {"status": "error", "message": "Проверка аккаунта отключена"}




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
FAV_FILE = DATA_DIR / "favorites.json"  # uid -> [{l,p,g}]
REQUESTS_FILE = DATA_DIR / "game_requests.json"  # list of requests
CHECK_STATUS_FILE = DATA_DIR / "check_status.json"  # login -> status
GIVEAWAY_FILE = DATA_DIR / "giveaway.json"
BUY_ORDERS_FILE = DATA_DIR / "buy_orders.json"  # оплаченные /buy
BUY_STARS_PRICE = int(os.getenv("BUY_STARS_PRICE", "15"))
  # текущий розыгрыш и победители
SHARED_TEXTS_FILE = DATA_DIR / "shared_texts.json"  # тексты, доступные по персональным deep-link ссылкам
CHECK_COOLDOWN = 60  # глобально: 1 проверка в минуту на весь бот
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
# Полный URL Mini App, например https://your-service.onrender.com/miniapp
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
if not WEBAPP_URL and WEBHOOK_URL:
    WEBAPP_URL = WEBHOOK_URL.rstrip("/") + "/miniapp"
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
last_global_check: float = 0.0
check_lock = False

# HTTP-прокси для проверки Steam (ротация)
STEAM_PROXIES = []

def _load_proxies() -> list:
    out = []
    seen = set()
    # 1) proxies.txt рядом с bot.py
    for candidate in (ROOT / "proxies.txt", Path("proxies.txt"), DATA_DIR / "proxies.txt"):
        try:
            if candidate.is_file():
                for ln in candidate.read_text(encoding="utf-8", errors="ignore").splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if not ln.startswith(("http://", "https://", "socks4://", "socks5://")):
                        ln = "http://" + ln
                    if ln not in seen:
                        seen.add(ln)
                        out.append(ln)
                break
        except Exception as e:
            print("proxies.txt load:", e)
    # 2) Env STEAM_PROXIES=url1,url2
    extra = os.getenv("STEAM_PROXIES", "").strip()
    if extra:
        for p in extra.split(","):
            p = p.strip()
            if not p:
                continue
            if not p.startswith(("http://", "https://", "socks4://", "socks5://")):
                p = "http://" + p
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out

STEAM_PROXIES = _load_proxies()
print(f"Loaded {len(STEAM_PROXIES)} proxies")
_proxy_idx = 0
_proxy_lock = threading.Lock()


def _next_proxy() -> str | None:
    global _proxy_idx
    if not STEAM_PROXIES:
        return None
    with _proxy_lock:
        p = STEAM_PROXIES[_proxy_idx % len(STEAM_PROXIES)]
        _proxy_idx += 1
        return p


def _proxy_dict(url: str | None) -> dict | None:
    if not url:
        return None
    # HTTP(S) и SOCKS4/5: requests шлёт и http, и https через один URL
    return {"http": url, "https": url}

pending_captcha: dict[int, dict] = {}
captcha_sessions: dict[int, object] = {}  # uid -> requests.Session
# сессии админов после ввода секрета (до рестарта)
admin_sessions: set[int] = set()
# ожидание текста рассылки / настройки
admin_wait: dict[int, str] = {}
# order_id -> {uid, username, name, charge_id, at, delivered}
buy_orders: dict[str, dict] = {}
# uid -> set of chat_ids selected for action
admin_selected: dict[int, set[int]] = {}
giveaway_lock = threading.RLock()
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
    s.setdefault("welcome_enabled", False)
    s.setdefault("welcome_payload", {})
    s.setdefault("custom_commands", {})
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
    mark = get_check_mark(a.get("l") or "")
    mark_line = f"\n\n{mark}" if mark else ""
    return (
        f"{head}🎮 <b>{esc(a.get('g') or '—')}</b>\n\n"
        f"👤 Логин:\n<code>{esc(a.get('l'))}</code>\n\n"
        f"🔑 Пароль:\n<code>{esc(a.get('p'))}</code>"
        f"{mark_line}"
    )




def load_check_status() -> dict:
    data = load_json(CHECK_STATUS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_check_status(data: dict) -> None:
    save_json(CHECK_STATUS_FILE, data)


def get_check_mark(login: str) -> str:
    st = load_check_status().get((login or "").lower())
    if not isinstance(st, dict):
        return ""
    status = st.get("status") or ""
    when = st.get("at") or ""
    who = st.get("by") or ""
    if status == "ok":
        line = f"✅ <b>Работает</b>"
    elif status == "fail":
        line = f"❌ <b>Не работает</b>"
    elif status == "2fa":
        line = f"❌ <b>Не работает (Steam Guard / 2FA)</b>"
    elif status == "captcha":
        line = f"⚠️ <b>Нужна капча</b> (не проверено до конца)"
    else:
        line = f"ℹ️ {esc(st.get('message') or status)}"
    extra = []
    if when:
        extra.append(when)
    if who:
        extra.append(f"@{esc(who)}" if not str(who).startswith("@") else esc(who))
    if extra:
        line += " · " + " · ".join(extra)
    return line


def set_check_result(login: str, status: str, message: str, by: str = "") -> None:
    from datetime import datetime, timezone, timedelta
    msk = timezone(timedelta(hours=3))
    data = load_check_status()
    data[(login or "").lower()] = {
        "status": status,
        "message": message,
        "at": datetime.now(msk).strftime("%d.%m.%Y %H:%M"),
        "by": (by or "").lstrip("@"),
    }
    save_check_status(data)


def load_favorites() -> dict:
    data = load_json(FAV_FILE, {})
    return data if isinstance(data, dict) else {}


def save_favorites(data: dict) -> None:
    save_json(FAV_FILE, data)


def get_user_favs(uid: int) -> list:
    data = load_favorites()
    lst = data.get(str(uid)) or []
    return lst if isinstance(lst, list) else []


def add_favorite(uid: int, acc: dict) -> tuple[bool, str]:
    login = (acc.get("l") or "").strip()
    if not login:
        return False, "Нет логина"
    data = load_favorites()
    key = str(uid)
    lst = data.get(key) or []
    if not isinstance(lst, list):
        lst = []
    for x in lst:
        if (x.get("l") or "").lower() == login.lower():
            return False, "Уже в избранном"
    lst.insert(0, {"l": acc.get("l"), "p": acc.get("p"), "g": acc.get("g")})
    if len(lst) > 50:
        lst = lst[:50]
    data[key] = lst
    save_favorites(data)
    return True, "Добавлено в избранное"


def remove_favorite(uid: int, login: str) -> bool:
    data = load_favorites()
    key = str(uid)
    lst = data.get(key) or []
    if not isinstance(lst, list):
        return False
    nl = [x for x in lst if (x.get("l") or "").lower() != login.lower()]
    if len(nl) == len(lst):
        return False
    data[key] = nl
    save_favorites(data)
    return True


def load_requests() -> list:
    data = load_json(REQUESTS_FILE, [])
    return data if isinstance(data, list) else []


def save_requests(items: list) -> None:
    save_json(REQUESTS_FILE, items)


def add_game_request(uid: int, username: str, game: str) -> None:
    items = load_requests()
    items.insert(0, {
        "uid": uid,
        "username": username or "",
        "game": game.strip()[:200],
        "at": __import__("datetime").datetime.now(
            __import__("datetime").timezone(__import__("datetime").timedelta(hours=3))
        ).strftime("%d.%m.%Y %H:%M"),
    })
    save_requests(items[:200])


def acc_kb_private(idx: int, page: int, in_fav: bool = False, with_check: bool = True) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    if not in_fav:
        kb.add(types.InlineKeyboardButton("⭐ В избранное", callback_data=f"fv:{idx}:{page}"))
    else:
        kb.add(types.InlineKeyboardButton("❌ Убрать из избранного", callback_data=f"fu:{idx}"))
    return kb


def acc_kb_public(idx: int, page: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    return kb


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
            f"🎲 <code>/random</code> — случайный аккаунт\n"
            f"🎲 <code>/random шутер</code> — случайный по жанру\n"
            f"⭐ <code>/избранное</code> — избранное (только личка)\n"
            f"🎮 <code>/заявка название</code> — запрос игры\n"
            f"🛠 <code>/обход</code> — ссылки на обходы EA, Ubisoft и Rockstar\n"
        )
    )
    kb = page_kb(key, 0)
    # Кнопка Mini App всегда сверху (в личке — WebApp, в группе — URL если WebApp недоступен)
    if WEBAPP_URL:
        top = types.InlineKeyboardMarkup()
        if chat_type == "private" or (chat_type is None and chat_id > 0):
            top.add(types.InlineKeyboardButton(
                "🎮 Открыть Mini App",
                web_app=types.WebAppInfo(url=WEBAPP_URL),
            ))
        else:
            # в группах WebApp-кнопка ограничена — даём обычную ссылку
            top.add(types.InlineKeyboardButton(
                "🎮 Открыть Mini App",
                url=WEBAPP_URL,
            ))
        for row in kb.keyboard:
            top.row(*row)
        kb = top
    if reply_to:
        bot.reply_to(reply_to, title, reply_markup=kb)
    else:
        bot.send_message(chat_id, title, reply_markup=kb)



def load_giveaway() -> dict:
    data = load_json(GIVEAWAY_FILE, {})
    if not isinstance(data, dict):
        return {}
    data.setdefault("active", False)
    data.setdefault("code", "")
    data.setdefault("limit", 0)
    data.setdefault("prize_text", "")
    data.setdefault("winners", [])
    return data


def save_giveaway(data: dict) -> None:
    save_json(GIVEAWAY_FILE, data)


def giveaway_status_text() -> str:
    data = load_giveaway()
    if not data.get("active"):
        return "Розыгрыш: <b>не активен</b>"
    winners = data.get("winners") or []
    limit = int(data.get("limit") or 0)
    return f"Розыгрыш: <b>активен</b>\nПолучили приз: <b>{len(winners)}/{limit}</b>"


def notify_giveaway_admins(user, winner_number: int, limit: int) -> None:
    username = getattr(user, "username", None)
    uid = int(getattr(user, "id", 0) or 0)
    first_name = getattr(user, "first_name", "") or ""
    username_text = f"@{username}" if username else "не указан"
    notice = (
        "🎁 <b>Новый участник розыгрыша!</b>\n\n"
        f"Юзер: <b>{esc(first_name)}</b>\n"
        f"Username: <b>{esc(username_text)}</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Место: <b>{winner_number}/{limit}</b>"
    )
    recipients = set(_ADMIN_IDS) | set(admin_sessions)
    for admin_id in recipients:
        try:
            bot.send_message(admin_id, notice)
        except Exception as e:
            print(f"giveaway admin notify failed {admin_id}: {e}")


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
    kb.add(types.InlineKeyboardButton("🎁 Создать розыгрыш", callback_data="adm:giveaway"))
    kb.add(types.InlineKeyboardButton("➕ Добавить аккаунт (ссылка)", callback_data="adm:sharetext"))
    kb.add(types.InlineKeyboardButton("👋 Приветствие новых", callback_data="adm:welcome"))
    kb.add(types.InlineKeyboardButton("⌨️ Свои команды", callback_data="adm:custom"))
    kb.add(types.InlineKeyboardButton("🚫 Забанить", callback_data="adm:ban"))
    kb.add(types.InlineKeyboardButton("✅ Разбанить", callback_data="adm:unban"))
    kb.add(types.InlineKeyboardButton("📋 Список банов", callback_data="adm:banlist"))
    kb.add(types.InlineKeyboardButton("🎮 Заявки на игры", callback_data="adm:requests"))
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
        f"{giveaway_status_text()}\n\n"
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

    # Персональная ссылка вида https://t.me/BotName?start=txt_TOKEN.
    # Telegram передаёт payload в сообщении /start TOKEN.
    start_parts = (message.text or "").split(maxsplit=1)
    if len(start_parts) > 1 and start_parts[0].split("@", 1)[0].casefold() == "/start":
        payload = start_parts[1].strip().split()[0]
        if payload.startswith("txt_"):
            token = payload[4:]
            shared = load_json(SHARED_TEXTS_FILE, {})
            item = shared.get(token) if isinstance(shared, dict) else None
            shared_text = item.get("text") if isinstance(item, dict) else None
            if isinstance(shared_text, str) and shared_text:
                try:
                    bot.send_message(message.chat.id, shared_text[:4096], parse_mode=None)
                except Exception as e:
                    print("shared text delivery failed:", repr(e))
                    bot.reply_to(message, "Не удалось отправить сохранённый текст. Попробуй ещё раз позже.")
            else:
                bot.reply_to(message, "Эта ссылка недействительна или текст больше не существует.")
            return

    if message.chat.type in ("group", "supergroup"):
        remember_chat(message.chat.id, message.chat.type)
        remember_user(message.chat.id, message.from_user)
    start_kb = None
    if WEBAPP_URL and message.chat.type == "private":
        start_kb = types.InlineKeyboardMarkup()
        start_kb.add(types.InlineKeyboardButton("🎮 Открыть Mini App", web_app=types.WebAppInfo(url=WEBAPP_URL)))
    bot.reply_to(
        message,
        "Список общих Steam-аккаунтов.\n\n"
        "<b>/accfree</b> - все аккаунты\n"
        "<b>/accfree forza</b> - поиск по игре\n"
        "<b>/обход</b> - ссылки на обходы EA, Ubisoft и Rockstar\n"
        "<b>/random</b> - случайный аккаунт\n"
        "<b>/random шутер</b> - по жанру (гонка, хоррор, рпг…)\n"
        "<b>/избранное</b> - только в личке\n"
        "<b>/заявка игра</b> - запрос на добавление\n"
        "<b>/подарок КОД</b> - получить приз в активном розыгрыше\n\n"
        "Работает в личке и в группах.",
        reply_markup=start_kb,
    )



@bot.message_handler(commands=["obhod", "обход", "bypass"])
def cmd_obhod(message: types.Message):
    """Показать кнопки со ссылками на материалы по обходу лаунчеров."""
    if reject_if_banned(message):
        return
    index_user(message.from_user)
    uid = message.from_user.id if message.from_user else 0
    now = time.time()
    left = CLICK_COOLDOWN - (now - last_cmd.get(uid, 0))
    if left > 0:
        bot.reply_to(message, f"⏳ Подожди ещё <b>{int(left) + 1}</b> сек.", parse_mode="HTML")
        return
    last_cmd[uid] = now

    if message.chat.type in ("group", "supergroup"):
        remember_chat(message.chat.id, message.chat.type)
        remember_user(message.chat.id, message.from_user)

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "Обход #1", url="https://t.me/MirArenDaff2/14776"
        ),
        types.InlineKeyboardButton(
            "Обход #2", url="https://t.me/MirArenDaff2/16357"
        ),
        types.InlineKeyboardButton(
            "Обход #3", url="https://github.com/LightnigFast/Project-Lightning/releases/tag/v5.0.11.0"
        ),
        types.InlineKeyboardButton(
            "Обход #4", url="https://github.com/onajlikezz/Nightlight-Launcher/releases/tag/NLLauncherV5.3.00"
        ),
    )

    bot.reply_to(
        message,
        "Обходы для: EA, Ubisoft, Rockstar",
        reply_markup=keyboard,
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




GENRE_KEYWORDS = {
    "шутер": ["cs2", "cs:", "counter-strike", "call of duty", "cod ", "battlefield", "doom", "overwatch", "valorant", "pubg", "apex", "rainbow", "far cry", "halo", "titanfall", "hunt"],
    "гонка": ["forza", "need for speed", "nfs", "assetto", "dirt", "f1", "racing", "gran turismo", "beamng", "crew"],
    "хоррор": ["phasmophobia", "outlast", "resident evil", "dead by daylight", "lethal company", "horror", "layers of fear", "amnesia", "dead space"],
    "рпг": ["witcher", "ведьмак", "skyrim", "elders scrolls", "dark souls", "elden ring", "divinity", "dragon age", "fallout", "diablo", "path of exile", "hogwarts"],
    "стратегия": ["civilization", "age of empires", "total war", "starcraft", "strategy", "company of heroes", "xcom", "cities skylines"],
    "приключения": ["uncharted", "tomb raider", "assassin", "red dead", "gta", "spider-man", "spiderman", "god of war", "horizon"],
    "выживание": ["rust", "ark", "minecraft", "valheim", "green hell", "the forest", "subnautica", "dont starve", "dayz"],
    "спорт": ["fifa", "ea fc", "nba", "nhl", "ufc", "football", "soccer", "rocket league"],
    "симулятор": ["simulator", "euro truck", "farming", "flight", "sims", "train"],
}


def accounts_by_genre(genre: str) -> list:
    g = (genre or "").strip().lower()
    if not g:
        return list(ACCOUNTS)
    keys = GENRE_KEYWORDS.get(g)
    if keys:
        out = []
        for a in ACCOUNTS:
            blob = f"{a.get('g') or ''} {a.get('l') or ''}".lower()
            if any(k in blob for k in keys):
                out.append(a)
        return out
    # свободный текст = как поиск
    return search_accounts(g)


@bot.message_handler(commands=["random", "rand", "случайный", "genre", "жанр"])
def cmd_random(message: types.Message):
    if reject_if_banned(message):
        return
    try:
        index_user(message.from_user)
        uid = message.from_user.id
        now = time.time()
        left = int(CLICK_COOLDOWN - (now - last_cmd.get(uid, 0)))
        if left > 0:
            bot.reply_to(message, f"⏳ Подожди ещё <b>{left}</b> сек.")
            return
        last_cmd[uid] = now

        if not ACCOUNTS:
            bot.reply_to(message, "База пуста.")
            return

        raw = (message.text or "").strip()
        parts = raw.split(maxsplit=1)
        genre = parts[1].strip() if len(parts) > 1 else ""

        if not genre:
            pool = list(ACCOUNTS)
            title = "🎲 <b>Случайный аккаунт</b>"
        else:
            pool = accounts_by_genre(genre)
            if not pool:
                genres = ", ".join(sorted(GENRE_KEYWORDS.keys()))
                bot.reply_to(
                    message,
                    f"По «{esc(genre)}» ничего нет.\n\n"
                    f"Жанры: <code>{genres}</code>\n"
                    f"Или: <code>/random gta</code>",
                )
                return
            title = f"🎲 <b>Случайный · {esc(genre)}</b> (из {len(pool)})"

        a = random.choice(pool)
        key = ctx_key(message.chat.id, uid)
        user_results[key] = {"list": [a], "q": f"random:{genre or 'all'}"}
        if message.chat.type == "private":
            kb = acc_kb_private(0, 0, in_fav=False, with_check=True)
        else:
            kb = acc_kb_public(0, 0)
        bot.reply_to(message, title + "\n\n" + fmt_acc(a, 1), reply_markup=kb)
    except Exception as e:
        print("cmd_random error:", e)
        try:
            bot.reply_to(message, f"Ошибка random: {esc(str(e))}")
        except Exception:
            pass


@bot.message_handler(commands=["fav", "favorites", "избранное"])
def cmd_fav(message: types.Message):
    if reject_if_banned(message):
        return
    index_user(message.from_user)
    if message.chat.type != "private":
        bot.reply_to(message, "⭐ Избранное доступно только в <b>личке</b> с ботом.")
        return
    uid = message.from_user.id
    favs = get_user_favs(uid)
    if not favs:
        bot.reply_to(
            message,
            "⭐ Избранное пусто.\n"
            "Открой аккаунт в личке и нажми «В избранное».",
        )
        return
    key = ctx_key(message.chat.id, uid)
    user_results[key] = {"list": favs, "q": "fav"}
    lines = [f"⭐ <b>Избранное</b>: {len(favs)}\n"]
    kb = types.InlineKeyboardMarkup()
    for i, a in enumerate(favs[:20]):
        g = a.get("g") or a.get("l") or "?"
        short = g if len(g) <= 36 else g[:33] + "..."
        kb.add(types.InlineKeyboardButton(f"{i+1}. {short}", callback_data=f"a:{i}:0"))
    bot.reply_to(message, "\n".join(lines), reply_markup=kb)


@bot.message_handler(commands=["request", "заявка", "want"])
def cmd_request(message: types.Message):
    if reject_if_banned(message):
        return
    index_user(message.from_user)
    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or len(parts[1].strip()) < 2:
        bot.reply_to(
            message,
            "🎮 Заявка на игру\n\n"
            "Формат: <code>/заявка название игры</code>\n"
            "Пример: <code>/заявка Red Dead Redemption 2</code>",
        )
        return
    game = parts[1].strip()
    uname = message.from_user.username or message.from_user.first_name or str(uid)
    add_game_request(uid, uname, game)
    bot.reply_to(
        message,
        f"✅ Заявка принята: <b>{esc(game)}</b>\n"
        "Админы увидят её в панели.",
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


def save_admin_payload(uid: int, step: str, message) -> None:
    """Сохраняет текст/фото для приветствия или кастомной команды и просит кнопки."""
    payload = {}
    if getattr(message, "photo", None):
        payload = {"type": "photo", "file_id": message.photo[-1].file_id,
                   "caption": (message.caption or "")[:1000]}
    elif getattr(message, "text", None):
        payload = {"type": "text", "text": message.text[:4000]}
    else:
        bot.reply_to(message, "Пришли текст или фото с подписью.")
        admin_wait[uid] = step
        return

    if step == "welcome_content":
        temp_key = "pending_welcome"
    else:
        temp_key = "pending_custom"
    # pending данные храним в settings, чтобы пережить несколько gunicorn workers
    st = load_settings()
    st[temp_key] = payload
    save_settings(st)
    admin_wait[uid] = "welcome_buttons" if step == "welcome_content" else "custom_buttons"
    bot.reply_to(message,
        "Теперь отправь кнопки в формате: Название | https://ссылка (каждая с новой строки).\\n"
        "Если кнопки не нужны — отправь /skip. Отмена: /cancel")


def make_payload_markup(lines: str):
    """Build inline URL/copy buttons.

    Supported formats (one button per line):
      Button title | https://example.com
      https://example.com | Button title  (reverse order is also accepted)
      Button title | text to copy | копировать
      Button title | copy:TEXT
      https://example.com Button title  (legacy convenient form)
    """
    kb = types.InlineKeyboardMarkup()
    added = 0
    copy_button_type = getattr(types, "CopyTextButton", None)

    for raw_line in (lines or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        parts = [part.strip().strip('"\'') for part in line.split("|")]
        label = ""
        action = ""
        mode = "url"

        if len(parts) >= 3 and parts[2].casefold() in ("копировать", "copy"):
            label, action, mode = parts[0], parts[1], "copy"
        elif len(parts) >= 2:
            first, second = parts[0], parts[1]
            if first.lower().startswith(("https://", "http://")):
                action, label = first, second
            else:
                label, action = first, second
        else:
            # Also accept: https://example.com Button title
            first_space = line.find(" ")
            if first_space > 0 and line[:first_space].lower().startswith(("https://", "http://")):
                action, label = line[:first_space].strip(), line[first_space + 1:].strip()
            else:
                continue

        if not label or not action:
            continue

        if mode == "copy" or action.casefold().startswith("copy:"):
            if action.casefold().startswith("copy:"):
                action = action[5:].strip()
            if not action:
                continue
            if copy_button_type is None:
                print("CopyTextButton unavailable: update pyTelegramBotAPI to >=4.29.0")
                continue
            kb.add(types.InlineKeyboardButton(
                text=label[:64],
                copy_text=copy_button_type(text=action[:256]),
            ))
            added += 1
            continue

        if action.lower().startswith(("https://", "http://")):
            kb.add(types.InlineKeyboardButton(text=label[:64], url=action[:2048]))
            added += 1
        else:
            print(f"Skipped invalid custom button line: {raw_line!r}")

    if lines and not added:
        print("No valid inline buttons parsed. Input was:", repr(lines))
    return kb if added else None


def send_payload(chat_id: int, payload: dict):
    if not isinstance(payload, dict):
        print("send_payload: invalid payload", repr(payload))
        return
    buttons_text = payload.get("buttons_text", "") or ""
    kb = make_payload_markup(buttons_text)
    try:
        if payload.get("type") == "photo" and payload.get("file_id"):
            bot.send_photo(chat_id, payload["file_id"], caption=payload.get("caption", "")[:1024],
                           reply_markup=kb, parse_mode="HTML")
        elif payload.get("type") == "text":
            bot.send_message(chat_id, payload.get("text", "")[:4000],
                             reply_markup=kb, parse_mode="HTML")
        else:
            print("send_payload: unknown payload type", repr(payload.get("type")))
    except Exception as e:
        print("send_payload Telegram error:", repr(e))
        raise


@bot.callback_query_handler(func=lambda c: True)
def on_callback(cq: types.CallbackQuery):
    data = cq.data or ""
    uid = cq.from_user.id

    if data.startswith("buyreply:"):
        if not is_admin(uid):
            bot.answer_callback_query(cq.id, "Только админ", show_alert=True)
            return
        order_id = data.split(":", 1)[1]
        orders = load_buy_orders()
        order = orders.get(order_id)
        if not order:
            bot.answer_callback_query(cq.id, "Заказ не найден", show_alert=True)
            return
        admin_wait[uid] = f"buy_deliver:{order_id}"
        bot.answer_callback_query(cq.id)
        bot.send_message(
            cq.message.chat.id if cq.message else uid,
            (
                "✍️ Пришли <b>текст</b> для покупателя "
                f"(ID <code>{order.get('uid')}</code>, "
                f"@{esc(order.get('username') or '—')}).\n"
                "Отмена: /cancel"
            ),
        )
        return


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



        if action == "welcome":
            admin_wait[uid] = "welcome_content"
            bot.answer_callback_query(cq.id)
            bot.send_message(chat_id, "👋 Пришли текст или фото с подписью для приветствия новых участников. HTML поддерживается. Отмена: /cancel")
            return

        if action == "custom":
            cmds = load_settings().get("custom_commands") or {}
            kb = types.InlineKeyboardMarkup()
            keys = sorted(str(k) for k in cmds.keys())
            listing = "\n".join(f"• <code>{esc(k)}</code>" for k in keys) if keys else "Пока нет команд."
            for i, key in enumerate(keys):
                kb.row(
                    types.InlineKeyboardButton(f"✏️ {key[:24]}", callback_data=f"adm:cmdedit:{i}"),
                    types.InlineKeyboardButton("🗑 Удалить", callback_data=f"adm:cmddelete:{i}"),
                )
            kb.add(types.InlineKeyboardButton("➕ Добавить команду", callback_data="adm:addcmd"))
            kb.add(types.InlineKeyboardButton("« Назад", callback_data="adm:panel"))
            bot.answer_callback_query(cq.id)
            bot.send_message(chat_id, "⌨️ <b>Свои команды</b>\n" + listing + "\n\nВыбери ✏️ для изменения или 🗑 для удаления.", reply_markup=kb)
            return

        if action.startswith("cmdedit:") or action.startswith("cmddelete:"):
            try:
                index = int(action.split(":", 1)[1])
                current = load_settings().get("custom_commands") or {}
                keys = sorted(str(k) for k in current.keys())
                trigger = keys[index]
            except (ValueError, IndexError, TypeError):
                bot.answer_callback_query(cq.id, "Команда уже отсутствует. Обнови список.", show_alert=True)
                return
            if action.startswith("cmddelete:"):
                st = load_settings()
                commands = st.get("custom_commands") or {}
                commands.pop(trigger, None)
                st["custom_commands"] = commands
                save_settings(st)
                bot.answer_callback_query(cq.id, "Команда удалена")
                remaining = sorted(str(k) for k in commands.keys())
                kb = types.InlineKeyboardMarkup()
                for i, key in enumerate(remaining):
                    kb.row(
                        types.InlineKeyboardButton(f"✏️ {key[:24]}", callback_data=f"adm:cmdedit:{i}"),
                        types.InlineKeyboardButton("🗑 Удалить", callback_data=f"adm:cmddelete:{i}"),
                    )
                kb.add(types.InlineKeyboardButton("➕ Добавить команду", callback_data="adm:addcmd"))
                kb.add(types.InlineKeyboardButton("« Назад", callback_data="adm:panel"))
                body = "⌨️ <b>Свои команды</b>\n" + ("\n".join(f"• <code>{esc(k)}</code>" for k in remaining) or "Пока нет команд.")
                try:
                    bot.edit_message_text(body, chat_id, cq.message.message_id, reply_markup=kb)
                except Exception:
                    bot.send_message(chat_id, body, reply_markup=kb)
                return
            admin_wait[uid] = "custom_edit_content:" + trigger
            bot.answer_callback_query(cq.id, "Режим изменения")
            bot.send_message(chat_id, f"✏️ Изменение команды <code>{esc(trigger)}</code>.\nПришли новый текст или фото с подписью. Затем сможешь заменить кнопки. Отмена: /cancel")
            return

        if action == "addcmd":
            admin_wait[uid] = "custom_name"
            bot.answer_callback_query(cq.id)
            bot.send_message(chat_id, "Введи команду без /, например <code>rules</code> или <code>!info</code>. Отмена: /cancel")
            return

        if action == "sharetext":
            admin_wait[uid] = "share_text"
            bot.answer_callback_query(cq.id)
            bot.send_message(
                chat_id,
                "➕ <b>Создание ссылки на текст</b>\n\n"
                "Пришли текст, который бот должен отправлять всем, кто откроет ссылку.\n"
                "Можно указать логин и пароль или любой другой текст. Максимум 4096 символов.\n"
                "Отмена: /cancel",
            )
            return

        if action == "giveaway":
            admin_wait[uid] = "giveaway_code"
            bot.answer_callback_query(cq.id)
            bot.send_message(
                chat_id,
                "🎁 <b>Создание розыгрыша — шаг 1/3</b>\n\n"
                "Пришли кодовое слово. Участники будут вводить его так: "
                "<code>/подарок КОД</code>\nОтмена: /cancel",
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

        if action == "requests":
            bot.answer_callback_query(cq.id)
            items = load_requests()
            if not items:
                bot.send_message(chat_id, "Заявок пока нет.")
                return
            lines = []
            kb = types.InlineKeyboardMarkup()
            for i, r in enumerate(items[:20]):
                un = r.get("username") or r.get("uid") or "?"
                game = r.get("game") or "?"
                short = game if len(game) <= 24 else game[:21] + "..."
                lines.append(
                    f"{i+1}. <b>{esc(game)}</b> — @{esc(str(un))} · {esc(r.get('at') or '')}"
                )
                kb.add(
                    types.InlineKeyboardButton(
                        f"📤 {i+1}. {short}",
                        callback_data=f"adm:reqsend:{i}",
                    )
                )
            more = f"\n… ещё {len(items)-20}" if len(items) > 20 else ""
            kb.add(types.InlineKeyboardButton("🗑 Очистить заявки", callback_data="adm:reqclear"))
            bot.send_message(
                chat_id,
                f"🎮 Заявки: <b>{len(items)}</b>\n"
                f"Нажми 📤 чтобы отправить акк человеку в личку.\n\n"
                + "\n".join(lines) + more,
                reply_markup=kb,
            )
            return

        if action.startswith("reqsend:"):
            try:
                idx = int(action.split(":")[1])
            except Exception:
                bot.answer_callback_query(cq.id, "Ошибка", show_alert=True)
                return
            items = load_requests()
            if idx < 0 or idx >= len(items):
                bot.answer_callback_query(cq.id, "Заявка устарела", show_alert=True)
                return
            r = items[idx]
            target = int(r.get("uid") or 0)
            game = r.get("game") or ""
            uname = r.get("username") or str(target)
            if not target:
                bot.answer_callback_query(cq.id, "Нет uid", show_alert=True)
                return
            admin_wait[uid] = f"req_acc:{target}:{idx}:{game}"
            bot.answer_callback_query(cq.id)
            bot.send_message(
                chat_id,
                f"📤 Заявка: <b>{esc(game)}</b>\n"
                f"Кому: @{esc(str(uname))} (<code>{target}</code>)\n\n"
                f"Напиши <b>любой ответ</b> одним сообщением "
                f"(акк, ссылка, текст — как угодно).\n"
                f"Это уйдёт человеку в личку.\n"
                f"Отмена: /cancel",
            )
            return

        if action == "reqclear":
            save_requests([])
            bot.answer_callback_query(cq.id, "Очищено")
            bot.send_message(chat_id, "Список заявок очищен.")
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
            page = int(parts[2]) if len(parts) > 2 else 0
        except Exception:
            bot.answer_callback_query(cq.id)
            return
        if idx < 0 or idx >= len(found):
            bot.answer_callback_query(cq.id, "Снова /accfree", show_alert=True)
            return
        a = found[idx]
        kb = None
        if cq.message and cq.message.chat.type == "private":
            favs = get_user_favs(uid)
            in_fav = any((x.get("l") or "").lower() == (a.get("l") or "").lower() for x in favs)
            kb = acc_kb_private(idx, page, in_fav=in_fav, with_check=True)
        else:
            kb = acc_kb_public(idx, page)
        bot.send_message(chat_id, fmt_acc(a, idx + 1), reply_markup=kb)
        bot.answer_callback_query(cq.id)
        return


@bot.message_handler(func=lambda m: bool(m.text) and m.chat.type in ("group", "supergroup"))
def on_group_custom_command(message: types.Message):
    """Пользовательские команды в группах: /rules, !rules или rules.

    Для /команд бот получает обновления и при включённом Privacy Mode.
    Для !команд/обычного текста Privacy Mode нужно отключить в BotFather.
    """
    if reject_if_banned(message):
        return
    remember_chat(message.chat.id, message.chat.type)
    raw_text = (message.text or "").strip()
    if not raw_text:
        return

    # Удаляем префикс / или ! и необязательное @имя_бота у slash-команды.
    command_text = raw_text
    if command_text.startswith("/") or command_text.startswith("!"):
        command_text = command_text[1:].split(None, 1)[0]
        command_text = command_text.split("@", 1)[0]
    else:
        # Для текстовых триггеров не используем дополнительные аргументы.
        command_text = command_text.split(None, 1)[0]

    settings_now = load_settings()
    commands = settings_now.get("custom_commands") or {}
    # Нормализуем сохранённый триггер так же, как входящее сообщение.
    # Раньше `!info` сохранялся с `!`, а при обработке сообщения префикс
    # удалялся, поэтому сравнение `!info` с `info` никогда не совпадало.
    incoming = command_text.strip().lstrip("/!").split("@", 1)[0].casefold()
    for trigger, payload in commands.items():
        saved_trigger = str(trigger).strip().lstrip("/!").split("@", 1)[0].casefold()
        if saved_trigger and saved_trigger == incoming:
            try:
                send_payload(message.chat.id, payload)
                print(f"custom command matched: {saved_trigger} in chat {message.chat.id}")
            except Exception as e:
                print("custom command send failed:", repr(e))
            return


@bot.message_handler(content_types=["photo"], func=lambda m: m.chat.type == "private")
def on_admin_photo(message: types.Message):
    uid = message.from_user.id if message.from_user else 0
    if not is_admin(uid) or uid not in admin_wait:
        return
    step = admin_wait.pop(uid)
    if step in ("welcome_content",) or step.startswith(("custom_content:", "custom_edit_content:")):
        if step.startswith(("custom_content:", "custom_edit_content:")):
            trigger = step.split(":", 1)[1]
            payload = {"type": "photo", "file_id": message.photo[-1].file_id,
                       "caption": (message.caption or "")[:1000]}
            st = load_settings()
            st["pending_custom"] = {"trigger": trigger, "payload": payload}
            save_settings(st)
            admin_wait[uid] = "custom_buttons"
            bot.reply_to(message, "Кнопки (каждая с новой строки): Название | https://ссылка. Для копирования: Название | текст | копировать. Можно также URL | Название. Либо /skip.")
        else:
            save_admin_payload(uid, step, message)


@bot.message_handler(content_types=["new_chat_members"])
def welcome_new_members(message: types.Message):
    remember_chat(message.chat.id, message.chat.type)
    st = load_settings()
    if not st.get("welcome_enabled"):
        return
    payload = st.get("welcome_payload") or {}
    for member in message.new_chat_members or []:
        if getattr(member, "is_bot", False):
            continue
        try:
            send_payload(message.chat.id, payload)
        except Exception as e:
            print("welcome send failed:", e)


@bot.message_handler(commands=["подарок", "Подарок", "gift"])
def cmd_giveaway(message: types.Message):
    if reject_if_banned(message):
        return
    user = message.from_user
    if not user:
        return
    index_user(user)
    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Использование: <code>/подарок КОДОВОЕ_СЛОВО</code>")
        return
    entered_code = parts[1].strip().split()[0]

    with giveaway_lock:
        giveaway = load_giveaway()
        if not giveaway.get("active"):
            bot.reply_to(message, "Сейчас активного розыгрыша нет.")
            return
        correct_code = str(giveaway.get("code") or "")
        if entered_code.casefold() != correct_code.casefold():
            bot.reply_to(message, "❌ Неверное кодовое слово.")
            return
        winners = giveaway.get("winners")
        if not isinstance(winners, list):
            winners = []
        uid = int(user.id)
        if any(int(item.get("uid", 0)) == uid for item in winners if isinstance(item, dict)):
            bot.reply_to(message, "Ты уже участвовал в этом розыгрыше.")
            return
        limit = int(giveaway.get("limit") or 0)
        if limit <= 0 or len(winners) >= limit:
            giveaway["active"] = False
            save_giveaway(giveaway)
            bot.reply_to(message, "🎁 Все призы уже разобраны.")
            return
        winner_number = len(winners) + 1
        winners.append({
            "uid": uid,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "at": int(time.time()),
        })
        giveaway["winners"] = winners
        if len(winners) >= limit:
            giveaway["active"] = False
        save_giveaway(giveaway)
        prize_text = str(giveaway.get("prize_text") or "")

    # Сначала пробуем отправить приз участнику в личку. Если он ещё не запускал
    # бота или запретил сообщения, отправляем приз ответом в текущем чате.
    try:
        bot.send_message(user.id, prize_text, parse_mode=None)
    except Exception as private_error:
        print(f"giveaway private prize send failed for {user.id}: {private_error}")
        try:
            bot.send_message(
                message.chat.id,
                prize_text,
                parse_mode=None,
                reply_to_message_id=message.message_id,
            )
        except Exception as chat_error:
            print(f"giveaway chat prize send failed for {user.id}: {chat_error}")
    notify_giveaway_admins(user, winner_number, limit)


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message: types.Message):
    admin_wait.pop(message.from_user.id, None)
    pending_captcha.pop(message.from_user.id, None)
    captcha_sessions.pop(message.from_user.id, None)
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

    # Пользовательские команды (личка)
    settings_now = load_settings()
    for trigger, payload in (settings_now.get("custom_commands") or {}).items():
        if text.casefold() == str(trigger).casefold():
            send_payload(message.chat.id, payload)
            return

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

        if step.startswith("buy_deliver:"):
            order_id = step.split(":", 1)[1]
            orders = load_buy_orders()
            order = orders.get(order_id)
            if not order:
                bot.reply_to(message, "Заказ не найден.")
                return
            body = (text or "").strip()
            if not body:
                bot.reply_to(message, "Пусто. Пришли текст или /cancel")
                admin_wait[uid] = step
                return
            target = int(order.get("uid") or 0)
            try:
                bot.send_message(target, body, parse_mode=None)
            except Exception as e:
                bot.reply_to(message, f"Не удалось отправить: {esc(str(e))}")
                admin_wait[uid] = step
                return
            order["delivered"] = True
            order["delivered_at"] = int(time.time())
            order["delivered_by"] = uid
            orders[order_id] = order
            save_buy_orders(orders)
            bot.reply_to(message, f"✅ Отправлено покупателю <code>{target}</code>")
            return

        if step == "share_text":
            shared_text = (message.text or "").strip()
            if not shared_text:
                bot.reply_to(message, "Текст не должен быть пустым. Пришли текст ещё раз.")
                admin_wait[uid] = "share_text"
                return
            if len(shared_text) > 4096:
                bot.reply_to(message, "Слишком длинный текст. Максимум — 4096 символов. Пришли сокращённый вариант.")
                admin_wait[uid] = "share_text"
                return
            try:
                bot_info = bot.get_me()
                if not bot_info.username:
                    raise RuntimeError("У бота не найден username")
                token = py_secrets.token_urlsafe(9)
                shared = load_json(SHARED_TEXTS_FILE, {})
                if not isinstance(shared, dict):
                    shared = {}
                while token in shared:
                    token = py_secrets.token_urlsafe(9)
                shared[token] = {
                    "text": shared_text,
                    "created_by": int(uid),
                    "created_at": int(time.time()),
                }
                save_json(SHARED_TEXTS_FILE, shared)
                share_url = f"https://t.me/{bot_info.username}?start=txt_{token}"
                bot.reply_to(
                    message,
                    "✅ <b>Ссылка создана!</b>\n\n"
                    f"<a href=\"{share_url}\">Открыть текст</a>\n\n"
                    f"<code>{share_url}</code>\n\n"
                    "Отправь эту ссылку человеку. Когда он откроет её и нажмёт START в Telegram, бот пришлёт сохранённый текст.",
                    disable_web_page_preview=True,
                    reply_markup=admin_kb(),
                )
            except Exception as e:
                print("shared text link creation failed:", repr(e))
                bot.reply_to(message, "Не удалось создать ссылку. Проверь, что бот доступен в Telegram, и попробуй ещё раз.")
                admin_wait[uid] = "share_text"
            return

        if step == "custom_name":
            trigger = text.strip()
            if not trigger or len(trigger) > 40 or trigger.startswith("/"):
                bot.reply_to(message, "Команда должна быть до 40 символов и без начального /. Повтори.")
                admin_wait[uid] = "custom_name"
                return
            admin_wait[uid] = "custom_content:" + trigger
            bot.reply_to(message, "Пришли текст или фото с подписью для команды.")
            return
        if step.startswith(("custom_content:", "custom_edit_content:")):
            trigger = step.split(":", 1)[1]
            payload = {"type": "text", "text": text[:4000]}
            st = load_settings()
            st["pending_custom"] = {"trigger": trigger, "payload": payload}
            save_settings(st)
            admin_wait[uid] = "custom_buttons"
            bot.reply_to(message, "Кнопки (каждая с новой строки): Название | https://ссылка. Для копирования: Название | текст | копировать. Можно также URL | Название. Либо /skip.")
            return
        if step == "custom_buttons":
            st = load_settings()
            pending = st.pop("pending_custom", {})
            if not isinstance(pending, dict) or not pending.get("trigger"):
                bot.reply_to(message, "Черновик команды потерян. Начни заново.")
                return
            payload = pending.get("payload") or {}
            payload["buttons_text"] = "" if text == "/skip" else text
            commands = st.get("custom_commands") or {}
            commands[pending["trigger"]] = payload
            st["custom_commands"] = commands
            save_settings(st)
            bot.reply_to(
                message,
                f"✅ Команда <code>{esc(pending['trigger'])}</code> сохранена.\n\n"
                f"В группе вызывай её так: <code>/{esc(pending['trigger'])}</code> "
                f"или <code>!{esc(pending['trigger'])}</code>.\n"
                "Если вариант с ! не срабатывает, отключи Privacy Mode через @BotFather.",
                reply_markup=admin_kb(),
            )
            return
        if step == "welcome_buttons":
            st = load_settings()
            payload = st.pop("pending_welcome", {})
            payload["buttons_text"] = "" if text == "/skip" else text
            st["welcome_payload"] = payload
            st["welcome_enabled"] = True
            save_settings(st)
            bot.reply_to(message, "✅ Приветствие сохранено и включено.", reply_markup=admin_kb())
            return
        if step == "welcome_content":
            save_admin_payload(uid, step, message)
            return
        if step == "giveaway_code":
            code = text.strip()
            if not code or len(code) > 64 or any(ch.isspace() or ch == ":" for ch in code):
                bot.reply_to(message, "Код должен быть одним словом длиной до 64 символов.")
                admin_wait[uid] = "giveaway_code"
                return
            admin_wait[uid] = f"giveaway_count:{code}"
            bot.reply_to(message, "🎁 <b>Шаг 2/3</b>\nСколько первых участников получат приз? Введи число от 1 до 10000.")
            return
        if step.startswith("giveaway_count:"):
            code = step.split(":", 1)[1]
            try:
                count = int(text)
                if count < 1 or count > 10000:
                    raise ValueError()
            except Exception:
                bot.reply_to(message, "Введи целое число от 1 до 10000.")
                admin_wait[uid] = step
                return
            admin_wait[uid] = f"giveaway_prize:{code}:{count}"
            bot.reply_to(message, "🎁 <b>Шаг 3/3</b>\nПришли текст, который бот отправит победителю.")
            return
        if step.startswith("giveaway_prize:"):
            parts = step.split(":", 2)
            code = parts[1]
            try:
                count = int(parts[2])
            except Exception:
                bot.reply_to(message, "Ошибка шага. Начни создание розыгрыша заново.")
                return
            prize_text = text.strip()
            if not prize_text:
                bot.reply_to(message, "Текст приза не может быть пустым.")
                admin_wait[uid] = step
                return
            with giveaway_lock:
                giveaway = {
                    "active": True,
                    "code": code,
                    "limit": count,
                    "prize_text": prize_text,
                    "winners": [],
                    "created_by": uid,
                    "created_at": int(time.time()),
                }
                save_giveaway(giveaway)
            bot.reply_to(
                message,
                "✅ <b>Розыгрыш создан!</b>\n\n"
                f"Код: <code>{esc(code)}</code>\n"
                f"Количество призов: <b>{count}</b>\n\n"
                f"Участники вводят: <code>/подарок {esc(code)}</code>\n"
                "Первые участники получат приз автоматически.",
                reply_markup=admin_kb(),
            )
            return
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

        if step.startswith("req_acc:"):
            # req_acc:target_uid:idx:game — любой текст/медиа-ответ админа
            parts = step.split(":", 3)
            try:
                target = int(parts[1])
                idx = int(parts[2])
                game = parts[3] if len(parts) > 3 else ""
            except Exception:
                bot.reply_to(message, "Ошибка заявки, начни снова.")
                return
            body = (text or "").strip()
            if not body:
                bot.reply_to(message, "Пустое сообщение. Напиши текст ответа.")
                admin_wait[uid] = step
                return
            # свободный текст админа
            dm = (
                f"📩 <b>Ответ на заявку</b>\n"
                f"Игра: <b>{esc(game or '—')}</b>\n\n"
                f"{body}"
            )
            try:
                bot.send_message(target, dm, parse_mode="HTML")
            except Exception as e:
                # если HTML сломался из-за символов — шлём как есть
                try:
                    bot.send_message(
                        target,
                        f"📩 Ответ на заявку\nИгра: {game or '—'}\n\n{body}",
                        parse_mode=None,
                    )
                except Exception as e2:
                    bot.reply_to(
                        message,
                        f"❌ Не удалось написать в личку "
                        f"(не запускал бота / блок):\n{esc(str(e2))}",
                    )
                    return
            items = load_requests()
            if 0 <= idx < len(items) and int(items[idx].get("uid") or 0) == target:
                items.pop(idx)
                save_requests(items)
            bot.reply_to(message, "✅ Отправлено в личку. Заявка закрыта.")
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





def validate_telegram_init_data(init_data: str) -> tuple[bool, dict]:
    """Проверяет подпись Telegram Mini App initData, не доверяя данным браузера."""
    if not init_data or not BOT_TOKEN:
        return False, {}
    from urllib.parse import parse_qsl
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received_hash = fields.pop("hash", "")
        if not received_hash:
            return False, {}
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return False, {}
        auth_date = int(fields.get("auth_date", "0"))
        if auth_date <= 0 or abs(time.time() - auth_date) > 86400:
            return False, {}
        user = json.loads(fields.get("user", "{}"))
        if not isinstance(user, dict) or not user.get("id"):
            return False, {}
        return True, user
    except Exception as e:
        print("Mini App initData validation:", repr(e))
        return False, {}


@app.get("/miniapp")
def miniapp_page():
    return send_from_directory(ROOT, "miniapp.html")


@app.get("/api/miniapp/accounts")
def miniapp_accounts():
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    valid, user = validate_telegram_init_data(init_data)
    if not valid:
        return jsonify({"ok": False, "error": "Открой каталог через Telegram-бота."}), 401
    query = (request.args.get("q") or "").strip().casefold()
    results = []
    for account in ACCOUNTS:
        game = str(account.get("g") or "")
        if query and query not in game.casefold():
            continue
        results.append({
            "game": game or "Без названия",
            "login": str(account.get("l") or ""),
            "password": str(account.get("p") or ""),
        })
    return jsonify({"ok": True, "total": len(results), "accounts": results})


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





# ===== /buy Stars =====

def load_buy_orders() -> dict:
    data = load_json(BUY_ORDERS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_buy_orders(data: dict) -> None:
    save_json(BUY_ORDERS_FILE, data)


def notify_admins_buy(order_id: str, user, charge_id: str, stars: int) -> None:
    uid = int(getattr(user, "id", 0) or 0)
    uname = getattr(user, "username", None) or ""
    name = getattr(user, "first_name", "") or ""
    uname_t = f"@{uname}" if uname else "нет"
    text = (
        "💳 <b>Новая оплата /buy</b>\n\n"
        f"Юзер: <b>{esc(name)}</b> ({esc(uname_t)})\n"
        f"ID: <code>{uid}</code>\n"
        f"Сумма: <b>{stars}</b> ⭐\n"
        f"Order: <code>{order_id}</code>\n"
        f"Charge: <code>{esc(charge_id)}</code>\n\n"
        "Нажми кнопку и пришли текст — бот отправит его покупателю."
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "✍️ Ответить покупателю",
        callback_data=f"buyreply:{order_id}",
    ))
    recipients = set(_ADMIN_IDS) | set(admin_sessions)
    if not recipients:
        print("BUY notify: no admins configured (set ADMIN_IDS)")
    for admin_id in recipients:
        try:
            bot.send_message(admin_id, text, reply_markup=kb)
        except Exception as e:
            print(f"buy admin notify {admin_id}:", e)


@bot.message_handler(commands=["buy", "купить", "Buy", "Купить"])
def cmd_buy(message: types.Message):
    if reject_if_banned(message):
        return
    if message.chat.type != "private":
        bot.reply_to(message, "Команда <b>/buy</b> работает только в личке с ботом.")
        return
    index_user(message.from_user)
    uid = message.from_user.id
    order_id = f"buy_{uid}_{int(time.time())}"
    # payload max 128 bytes
    payload = order_id[:120]
    title = "Случайный аккаунт"
    description = (
        "После оплаты в течение пары минут вы получите рандомный аккаунт (не общий). "
        "Доступ придёт сообщением от бота."
    )
    prices = [types.LabeledPrice(label="Оплатить", amount=BUY_STARS_PRICE)]
    try:
        bot.send_invoice(
            message.chat.id,
            title=title,
            description=description,
            invoice_payload=payload,
            provider_token="",  # Stars
            currency="XTR",
            prices=prices,
            start_parameter="buy_account",
        )
    except Exception as e:
        print("send_invoice failed:", e)
        bot.reply_to(
            message,
            "Не удалось создать счёт. Проверь, что бот принимает Stars, "
            "или напиши админу.\n"
            f"<code>{esc(str(e)[:200])}</code>",
        )


@bot.pre_checkout_query_handler(func=lambda q: True)
def on_pre_checkout(pre: types.PreCheckoutQuery):
    try:
        # только наши /buy
        ok = bool(pre.invoice_payload and str(pre.invoice_payload).startswith("buy_"))
        if not ok:
            bot.answer_pre_checkout_query(pre.id, ok=False, error_message="Неизвестный заказ")
            return
        if pre.currency != "XTR":
            bot.answer_pre_checkout_query(pre.id, ok=False, error_message="Только Stars")
            return
        bot.answer_pre_checkout_query(pre.id, ok=True)
    except Exception as e:
        print("pre_checkout:", e)
        try:
            bot.answer_pre_checkout_query(pre.id, ok=False, error_message="Ошибка сервера")
        except Exception:
            pass


@bot.message_handler(content_types=["successful_payment"])
def on_successful_payment(message: types.Message):
    sp = message.successful_payment
    if not sp:
        return
    payload = sp.invoice_payload or ""
    if not payload.startswith("buy_"):
        return
    uid = message.from_user.id
    charge_id = sp.telegram_payment_charge_id or ""
    stars = int(sp.total_amount or 0)
    order_id = payload

    orders = load_buy_orders()
    # защита от двойной доставки
    if any(o.get("charge_id") == charge_id for o in orders.values()):
        bot.send_message(message.chat.id, "Оплата уже обработана. Ждите сообщение с аккаунтом.")
        return

    orders[order_id] = {
        "uid": uid,
        "username": message.from_user.username or "",
        "name": message.from_user.first_name or "",
        "charge_id": charge_id,
        "stars": stars,
        "at": int(time.time()),
        "delivered": False,
    }
    save_buy_orders(orders)

    bot.send_message(
        message.chat.id,
        "✅ <b>Оплата прошла!</b>\n\n"
        "В течение пары минут пришлём <b>рандомный аккаунт (не общий)</b>.\n"
        "Ожидайте сообщение от бота.",
    )
    notify_admins_buy(order_id, message.from_user, charge_id, stars)



def setup_bot_commands() -> None:
    """Команды в меню Telegram (появляются при вводе /)."""
    cmds = [
        types.BotCommand("start", "Старт и справка"),
        types.BotCommand("accfree", "Список и поиск аккаунтов"),
        types.BotCommand("buy", "Купить аккаунт за 15 Stars"),
        types.BotCommand("random", "Случайный аккаунт"),
        types.BotCommand("fav", "Избранное (личка)"),
        types.BotCommand("request", "Заявка на игру"),
        types.BotCommand("obhod", "Обходы лаунчеров"),
        types.BotCommand("gift", "Розыгрыш / подарок"),
        types.BotCommand("cancel", "Отмена ввода"),
    ]
    try:
        bot.set_my_commands(cmds)
        print("Bot commands menu set:", len(cmds))
    except Exception as e:
        print("set_my_commands failed:", e)


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
setup_bot_commands()
_t = threading.Thread(target=reminder_loop, name="reminder", daemon=True)
_t.start()
print(
    f"Ready | accounts={len(ACCOUNTS)} | chats={len(known_chats)} | "
    f"admin_secret={'yes' if _ADMIN_SECRET else 'NO'}"
)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
