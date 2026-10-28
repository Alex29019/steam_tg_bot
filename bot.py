"""
Steam TG Bot — Web Service
Start: gunicorn -b 0.0.0.0:$PORT bot:app
Env: BOT_TOKEN, WEBHOOK_URL
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import telebot
from flask import Flask, request
from telebot import types

# ===== steam check (inlined) =====
import base64
import time
from typing import Any

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


def _der_length(n: int) -> bytes:
    if n < 128:
        return bytes([n])
    b = []
    while n:
        b.insert(0, n & 0xFF)
        n >>= 8
    return bytes([0x80 | len(b), *b])


def _der_tag(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(content)) + content


def _der_int(buf: bytes) -> bytes:
    if buf and (buf[0] & 0x80):
        buf = b"\x00" + buf
    return _der_tag(0x02, buf)


def build_rsa_pem(mod_hex: str, exp_hex: str) -> bytes:
    mod = bytes.fromhex(mod_hex)
    exp = bytes.fromhex(exp_hex)
    rsa_key = _der_tag(0x30, _der_int(mod) + _der_int(exp))
    alg = _der_tag(0x30, _der_tag(0x06, bytes.fromhex("2a864886f70d010101")) + _der_tag(0x05, b""))
    spki = _der_tag(0x30, alg + _der_tag(0x03, b"\x00" + rsa_key))
    b64 = base64.b64encode(spki).decode()
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return ("-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----").encode()


def encrypt_password(password: str, mod_hex: str, exp_hex: str) -> str:
    key = RSA.import_key(build_rsa_pem(mod_hex, exp_hex))
    enc = PKCS1_v1_5.new(key).encrypt(password.encode("utf-8"))
    return base64.b64encode(enc).decode()


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


def check_steam(username: str, password: str, captcha_gid: str = "-1", captcha_text: str = "") -> dict[str, Any]:
    """
    Steam login check.
    success / requires_twofactor / emailauth → working
    """
    headers = {
        "User-Agent": UA,
        "Referer": "https://steamcommunity.com/login/home/?goto=",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://steamcommunity.com",
        "X-Requested-With": "XMLHttpRequest",
    }
    session = requests.Session()
    session.headers.update(headers)

    try:
        r = session.get(
            "https://steamcommunity.com/login/getrsakey/",
            params={"username": username, "donotcache": int(time.time() * 1000)},
            timeout=20,
        )
        rsa = r.json()
    except Exception as e:
        return {"status": "error", "message": f"Ошибка сети (RSA)"}

    if not isinstance(rsa, dict) or not rsa.get("success"):
        return {"status": "error", "message": "Аккаунт не работает"}

    try:
        enc_pass = encrypt_password(password, rsa["publickey_mod"], rsa["publickey_exp"])
    except Exception:
        return {"status": "error", "message": "Ошибка шифрования пароля"}

    payload = {
        "username": username,
        "password": enc_pass,
        "twofactorcode": "",
        "emailauth": "",
        "loginfriendlyname": "",
        "captchagid": captcha_gid or "-1",
        "captcha_text": captcha_text or "",
        "emailsteamid": "",
        "rsatimestamp": str(rsa.get("timestamp", "")),
        "remember_login": "false",
        "donotcache": str(int(time.time() * 1000)),
    }

    try:
        r = session.post(
            "https://steamcommunity.com/login/dologin/",
            data=payload,
            timeout=20,
        )
        # sometimes HTML / rate limit
        try:
            data = r.json()
        except Exception:
            body = (r.text or "")[:200].lower()
            if "too many" in body or "rate" in body:
                return {"status": "error", "message": "Steam лимит — подождите 1–2 мин"}
            return {"status": "error", "message": "Непонятный ответ Steam"}
    except Exception:
        return {"status": "error", "message": "Ошибка сети (login)"}

    if isinstance(data, str):
        return {"status": "error", "message": "Steam лимит — подождите 1–2 мин"}
    if not isinstance(data, dict):
        return {"status": "error", "message": "Аккаунт не работает"}

    # Full login OK
    if data.get("success") is True:
        return {"status": "success", "message": "Аккаунт работает"}

    # Password correct, Guard / email needed → account WORKS
    if (
        data.get("requires_twofactor")
        or data.get("emailauth_needed")
        or data.get("emailsteamid")
        or data.get("requires_twofactor") is True
    ):
        return {"status": "success", "message": "Аккаунт работает (Steam Guard / 2FA)"}

    # captcha (any form)
    if data.get("captcha_needed") or data.get("captcha_gid") not in (None, "", "-1", -1):
        if data.get("captcha_needed") or (not data.get("success") and data.get("captcha_gid")):
            return {
                "status": "captcha",
                "message": "Требуется капча",
                "captcha_gid": str(data.get("captcha_gid") or "-1"),
            }

    msg = str(data.get("message") or data.get("strError") or "")
    msg_l = msg.lower()

    # wrong password / account
    if any(x in msg_l for x in ("incorrect", "invalid", "password", "неверн", "ошибка")):
        return {"status": "error", "message": "Аккаунт не работает"}

    if "too many" in msg_l or "rate" in msg_l or "try again" in msg_l:
        return {"status": "error", "message": "Steam лимит — подождите 1–2 мин"}

    # unknown but has transfer_parameters / login_complete hints
    if data.get("login_complete") or data.get("transfer_urls") or data.get("transfer_parameters"):
        return {"status": "success", "message": "Аккаунт работает"}

    # If message empty but not clear failure — still report not working with debug hint
    if msg:
        return {"status": "error", "message": f"Аккаунт не работает ({msg[:80]})"}
    return {"status": "error", "message": "Аккаунт не работает"}


# ===== end steam check =====


ROOT = Path(__file__).resolve().parent
ACCOUNTS = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
STATUS_FILE = ROOT / "check_status.json"
USER_ACCOUNTS_FILE = ROOT / "user_accounts.json"
PAGE_SIZE = 8
CHECK_COOLDOWN = 20

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not set")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
MSK = timezone(timedelta(hours=3))

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# search context: uid -> {"list": [...], "source": "main"|"user"}
user_results: dict[int, dict] = {}
last_check: dict[int, float] = {}
pending_captcha: dict[int, dict] = {}
# uid -> step add flow
add_state: dict[int, dict] = {}


def now_str() -> str:
    return datetime.now(MSK).strftime("%d.%m.%Y %H:%M")


def esc(s: str) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path: Path, data) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    except Exception as e:
        print("save error", path, e)


def load_status() -> dict:
    return load_json(STATUS_FILE, {})


def save_status(data: dict) -> None:
    save_json(STATUS_FILE, data)


def set_check_result(login: str, status: str, message: str = "") -> dict:
    data = load_status()
    entry = {"status": status, "at": now_str(), "message": message or ""}
    data[login] = entry
    save_status(data)
    return entry


def get_check_result(login: str) -> dict | None:
    return load_status().get(login)


def status_line(login: str) -> str:
    st = get_check_result(login)
    if not st:
        return "➖ Ещё не проверяли"
    s = st.get("status")
    at = st.get("at") or "?"
    if s == "ok":
        return f"✅ Проверен: <b>работает</b> · {esc(at)}"
    if s == "bad":
        return f"❌ Проверен: <b>не работает</b> · {esc(at)}"
    return f"⚠️ Проверен: {esc(st.get('message') or s)} · {esc(at)}"


def load_user_accounts() -> list:
    data = load_json(USER_ACCOUNTS_FILE, [])
    return data if isinstance(data, list) else []


def save_user_accounts(items: list) -> None:
    save_json(USER_ACCOUNTS_FILE, items)


def login_exists_anywhere(login: str) -> str | None:
    """Return where login exists: main|user|None"""
    login_l = (login or "").strip().lower()
    for a in ACCOUNTS:
        if (a.get("l") or "").lower() == login_l:
            return "main"
    for a in load_user_accounts():
        if (a.get("l") or "").lower() == login_l:
            return "user"
    return None


def search_in(pool: list, query: str) -> list:
    q = (query or "").strip().lower()
    if len(q) < 2:
        return []
    out = []
    for a in pool:
        if q in (a.get("l") or "").lower() or q in (a.get("g") or "").lower():
            out.append(a)
            if len(out) >= 300:
                break
    return out


def get_ctx(uid: int) -> dict:
    return user_results.get(uid) or {"list": [], "source": "main"}


def set_ctx(uid: int, items: list, source: str) -> None:
    user_results[uid] = {"list": items, "source": source}


def page_kb(uid: int, page: int) -> types.InlineKeyboardMarkup:
    ctx = get_ctx(uid)
    found = ctx.get("list") or []
    source = ctx.get("source") or "main"
    pages = max(1, (len(found) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    kb = types.InlineKeyboardMarkup()
    start = page * PAGE_SIZE
    for i, a in enumerate(found[start : start + PAGE_SIZE]):
        idx = start + i
        games = a.get("g") or a.get("l") or "?"
        short = games if len(games) <= 36 else games[:33] + "..."
        st = get_check_result(a.get("l") or "")
        mark = ""
        if st:
            if st.get("status") == "ok":
                mark = "✅ "
            elif st.get("status") == "bad":
                mark = "❌ "
            else:
                mark = "⚠️ "
        prefix = "👥 " if source == "user" else ""
        kb.add(types.InlineKeyboardButton(
            f"{mark}{prefix}{idx + 1}. {short}",
            callback_data=f"a:{idx}:{page}",
        ))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("← Назад", callback_data=f"p:{page - 1}"))
    nav.append(types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(types.InlineKeyboardButton("Вперёд →", callback_data=f"p:{page + 1}"))
    if nav:
        kb.row(*nav)
    return kb


def acc_kb(idx: int, page: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Проверить", callback_data=f"c:{idx}"))
    kb.add(types.InlineKeyboardButton("« К списку", callback_data=f"p:{page}"))
    return kb


def fmt_acc(a: dict, n: int | None = None, source: str = "main") -> str:
    head = f"<b>#{n}</b>\n" if n is not None else ""
    tag = "👥 <i>От пользователей</i>\n" if source == "user" else ""
    extra = ""
    if source == "user":
        by = a.get("by") or "?"
        added = a.get("added") or "?"
        extra = f"\n📎 Добавил: {esc(str(by))} · {esc(str(added))}"
    return (
        f"{head}{tag}🎮 <b>{esc(a.get('g') or '—')}</b>\n\n"
        f"👤 Логин:\n<code>{esc(a.get('l'))}</code>\n\n"
        f"🔑 Пароль:\n<code>{esc(a.get('p'))}</code>\n\n"
        f"{status_line(a.get('l') or '')}{extra}"
    )


def fetch_captcha_image(gid: str) -> bytes | None:
    if not gid or gid == "-1":
        return None
    try:
        r = requests.get(
            f"https://steamcommunity.com/login/rendercaptcha/?gid={gid}",
            headers={
                "User-Agent": UA,
                "Referer": "https://steamcommunity.com/login/home/",
                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
            },
            timeout=20,
        )
        if r.status_code == 200 and r.content and len(r.content) > 100:
            return r.content
    except Exception as e:
        print("captcha image error:", e)
    return None


def apply_result(login: str, result: dict) -> str:
    status = result.get("status")
    msg = result.get("message") or status
    if status == "success":
        set_check_result(login, "ok", msg)
        return f"✅ <b>{esc(msg)}</b>\n👤 <code>{esc(login)}</code>\n\n{status_line(login)}"
    if status == "captcha":
        return "captcha"
    set_check_result(login, "bad", msg)
    return f"❌ <b>{esc(msg)}</b>\n👤 <code>{esc(login)}</code>\n\n{status_line(login)}"


def run_check(chat_id: int, uid: int, login: str, password: str, captcha_gid: str = "-1", captcha_text: str = "", after_ok=None):
    """Единая проверка: /check, кнопка Проверить, /add — везде одна логика + капча."""
    wait = bot.send_message(chat_id, "⏳ Проверка Steam...")
    result = check_steam(login, password, captcha_gid, captcha_text)
    status = result.get("status")

    if status == "captcha":
        gid = str(result.get("captcha_gid") or "-1")
        st = add_state.get(uid) or {}
        pending_captcha[uid] = {
            "login": login,
            "password": password,
            "captcha_gid": gid,
            "after_ok": after_ok is not None,
            # контекст /add (если есть)
            "is_add": after_ok is not None or st.get("step") == "checking",
            "games": st.get("games") or "",
            "by": st.get("by") or "",
        }
        if after_ok is not None or st.get("step") == "checking":
            add_state[uid] = {
                **st,
                "step": "checking",
                "await_captcha_add": True,
                "login": login,
                "password": password,
            }
        hint = (
            "⚠️ Нужна <b>капча Steam</b>\n\n"
            "Отправь символы с картинки <b>следующим сообщением</b>.\n"
            "Это работает для /check, кнопки «Проверить» и /add.\n"
            "Отмена: /cancel"
        )
        try:
            bot.edit_message_text(hint, chat_id, wait.message_id)
        except Exception:
            bot.send_message(chat_id, hint)
        img = fetch_captcha_image(gid)
        if img:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🔄 Обновить капчу", callback_data="rc:0"))
            bot.send_photo(
                chat_id,
                img,
                caption="🔤 Напиши текст с картинки",
                reply_markup=kb,
            )
        else:
            bot.send_message(chat_id, "Картинку капчи загрузить не удалось. /cancel и попробуй снова.")
        return

    if status == "success":
        set_check_result(login, "ok", result.get("message") or "")
        if after_ok:
            try:
                bot.delete_message(chat_id, wait.message_id)
            except Exception:
                pass
            after_ok(login, password)
            return
        text = apply_result(login, result)
    else:
        set_check_result(login, "bad", result.get("message") or "")
        text = apply_result(login, result)
        if after_ok:
            text = (
                "❌ Аккаунт не работает — в раздел пользователей <b>не добавлен</b>.\n"
                + text
            )
    try:
        bot.edit_message_text(text, chat_id, wait.message_id)
    except Exception:
        bot.send_message(chat_id, text)



def finish_add_account(chat_id: int, uid: int, login: str, password: str, games: str, username: str):
    where = login_exists_anywhere(login)
    if where == "main":
        bot.send_message(chat_id, "❌ Этот логин уже есть в <b>основном</b> списке.")
        add_state.pop(uid, None)
        return
    if where == "user":
        bot.send_message(chat_id, "❌ Этот логин уже есть в разделе <b>от пользователей</b>.")
        add_state.pop(uid, None)
        return
    items = load_user_accounts()
    items.insert(0, {
        "l": login.strip(),
        "p": password,
        "g": games.strip(),
        "by": username or str(uid),
        "added": now_str(),
        "uid": uid,
    })
    save_user_accounts(items)
    set_check_result(login.strip(), "ok", "Добавлен пользователем")
    add_state.pop(uid, None)
    bot.send_message(
        chat_id,
        f"✅ Аккаунт добавлен в раздел <b>От пользователей</b> (видно всем).\n\n"
        f"👤 <code>{esc(login)}</code>\n🎮 {esc(games)}\n\n"
        f"Смотреть: /users",
    )


# ----- commands -----

@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    bot.reply_to(
        message,
        f"Steam Accounts Bot\nОсновных: <b>{len(ACCOUNTS)}</b> · "
        f"От пользователей: <b>{len(load_user_accounts())}</b>\n\n"
        f"<b>Основной список</b>\n"
        f"• Напиши игру или /find\n"
        f"• /random\n"
        f"• /catalog — весь основной список\n"
        f"• /check логин пароль\n\n"
        f"<b>От пользователей</b> (отдельный раздел)\n"
        f"• /users — все добавленные\n"
        f"• /ufind игра — поиск там\n"
        f"• /add — добавить свой аккаунт\n\n"
        f"Добавление: аккаунт должен <b>работать</b> и <b>не быть</b> в списках. "
        f"Укажи логин, пароль и игры. Проверка раз в 20 сек.",
    )


@bot.message_handler(commands=["random"])
def cmd_random(message: types.Message):
    a = random.choice(ACCOUNTS)
    set_ctx(message.from_user.id, [a], "main")
    bot.reply_to(message, fmt_acc(a, 1, "main"), reply_markup=acc_kb(0, 0))


@bot.message_handler(commands=["catalog", "all", "list"])
def cmd_catalog(message: types.Message):
    """Каталог всех аккаунтов основного списка."""
    uid = message.from_user.id
    set_ctx(uid, ACCOUNTS, "main")
    bot.reply_to(
        message,
        f"📂 <b>Каталог основного списка</b>\n"
        f"Всего: <b>{len(ACCOUNTS)}</b>\n"
        f"Листай страницами или ищи через /find:",
        reply_markup=page_kb(uid, 0),
    )


@bot.message_handler(commands=["find"])
def cmd_find(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    do_search(message, parts[1].strip() if len(parts) > 1 else "", source="main")


@bot.message_handler(commands=["users", "user"])
def cmd_users(message: types.Message):
    items = load_user_accounts()
    if not items:
        bot.reply_to(message, "Раздел пуст. Добавь первый аккаунт: /add")
        return
    uid = message.from_user.id
    set_ctx(uid, items, "user")
    bot.reply_to(
        message,
        f"👥 <b>От пользователей</b>: {len(items)}\nВыбери аккаунт:",
        reply_markup=page_kb(uid, 0),
    )


@bot.message_handler(commands=["ufind"])
def cmd_ufind(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    do_search(message, parts[1].strip() if len(parts) > 1 else "", source="user")


@bot.message_handler(commands=["add"])
def cmd_add(message: types.Message):
    uid = message.from_user.id
    add_state[uid] = {"step": "login"}
    bot.reply_to(
        message,
        "➕ Добавление в раздел <b>От пользователей</b>\n\n"
        "1) Отправь <b>логин</b>\n"
        "2) Потом <b>пароль</b>\n"
        "3) Потом <b>игры</b> (текстом)\n"
        "4) Бот проверит Steam — только рабочий аккаунт попадёт в список\n\n"
        "Логин не должен быть в основном списке или уже добавлен.\n"
        "Отмена: /cancel",
    )


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message: types.Message):
    add_state.pop(message.from_user.id, None)
    pending_captcha.pop(message.from_user.id, None)
    bot.reply_to(message, "Отменено.")


@bot.message_handler(commands=["check"])
def cmd_check(message: types.Message):
    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "Формат: <code>/check логин пароль</code>")
        return
    login, password = parts[1], parts[2]
    now = time.time()
    left = int(CHECK_COOLDOWN - (now - last_check.get(uid, 0)))
    if left > 0:
        bot.reply_to(message, f"⏳ Подожди ещё <b>{left}</b> сек.")
        return
    last_check[uid] = now
    run_check(message.chat.id, uid, login, password)


def do_search(message: types.Message, q: str, source: str = "main"):
    if len(q) < 2:
        bot.reply_to(message, "Минимум 2 символа.")
        return
    pool = ACCOUNTS if source == "main" else load_user_accounts()
    found = search_in(pool, q)
    if not found:
        bot.reply_to(message, f"Ничего не найдено: <b>{esc(q)}</b>")
        return
    uid = message.from_user.id
    set_ctx(uid, found, source)
    title = "Основной" if source == "main" else "От пользователей"
    bot.reply_to(
        message,
        f"<b>{title}</b> · найдено: <b>{len(found)}</b>\nВыбери аккаунт:",
        reply_markup=page_kb(uid, 0),
    )


@bot.callback_query_handler(func=lambda c: True)
def on_callback(cq: types.CallbackQuery):
    data = cq.data or ""
    uid = cq.from_user.id
    ctx = get_ctx(uid)
    found = ctx.get("list") or []
    source = ctx.get("source") or "main"

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
            bot.answer_callback_query(cq.id, "Сначала поиск", show_alert=True)
            return
        try:
            bot.edit_message_reply_markup(
                cq.message.chat.id, cq.message.message_id, reply_markup=page_kb(uid, page)
            )
        except Exception:
            bot.send_message(cq.message.chat.id, "Список:", reply_markup=page_kb(uid, page))
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
            bot.answer_callback_query(cq.id, "Сделай поиск снова", show_alert=True)
            return
        a = found[idx]
        bot.send_message(cq.message.chat.id, fmt_acc(a, idx + 1, source), reply_markup=acc_kb(idx, page))
        bot.answer_callback_query(cq.id)
        return

    # refresh captcha
    if data.startswith("rc:"):
        pend = pending_captcha.get(uid)
        if not pend:
            bot.answer_callback_query(cq.id, "Нет активной капчи", show_alert=True)
            return
        bot.answer_callback_query(cq.id, "Новая проверка...")
        st = add_state.get(uid) or {}
        after = None
        if pend.get("after_ok") or st.get("await_captcha_add") or st.get("step") == "checking":
            def after(l, p, _uid=uid, _chat=cq.message.chat.id, _st=st):
                games = _st.get("games") or "?"
                uname = cq.from_user.username or cq.from_user.first_name or str(_uid)
                finish_add_account(_chat, _uid, l, p, games, uname)
            after = after
        run_check(
            cq.message.chat.id,
            uid,
            pend.get("login") or "",
            pend.get("password") or "",
            after_ok=after,
        )
        return

    if data.startswith("c:"):
        try:
            idx = int(data.split(":")[1])
        except Exception:
            bot.answer_callback_query(cq.id)
            return
        if idx < 0 or idx >= len(found):
            bot.answer_callback_query(cq.id, "Сделай поиск снова", show_alert=True)
            return
        now = time.time()
        left = int(CHECK_COOLDOWN - (now - last_check.get(uid, 0)))
        if left > 0:
            bot.answer_callback_query(cq.id, f"Подожди {left} сек.", show_alert=True)
            return
        a = found[idx]
        last_check[uid] = now
        bot.answer_callback_query(cq.id)
        run_check(cq.message.chat.id, uid, a.get("l") or "", a.get("p") or "")
        return

    bot.answer_callback_query(cq.id)


@bot.message_handler(func=lambda m: bool(m.text) and not (m.text or "").startswith("/"))
def on_text(message: types.Message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    # captcha reply (и для /check, и для кнопки, и для /add)
    if uid in pending_captcha:
        pend = pending_captcha.pop(uid)
        login = pend.get("login") or ""
        password = pend.get("password") or ""
        gid = pend.get("captcha_gid") or "-1"
        st = add_state.get(uid) or {}
        is_add = (
            pend.get("is_add")
            or pend.get("after_ok")
            or st.get("await_captcha_add")
            or st.get("step") == "checking"
        )
        if is_add:
            games = pend.get("games") or st.get("games") or "?"
            uname = (
                pend.get("by")
                or st.get("by")
                or message.from_user.username
                or message.from_user.first_name
                or str(uid)
            )

            def after_ok(l, p, _games=games, _uname=uname):
                finish_add_account(message.chat.id, uid, l, p, _games, _uname)

            bot.reply_to(message, "⏳ Проверяю капчу и аккаунт для добавления...")
            run_check(message.chat.id, uid, login, password, gid, text, after_ok=after_ok)
            return
        bot.reply_to(message, "⏳ Проверяю с капчей...")
        run_check(message.chat.id, uid, login, password, gid, text)
        return

    # /add wizard
    st = add_state.get(uid)
    if st:
        step = st.get("step")
        if step == "login":
            login = text
            if len(login) < 2:
                bot.reply_to(message, "Слишком короткий логин. Ещё раз:")
                return
            where = login_exists_anywhere(login)
            if where == "main":
                bot.reply_to(message, "❌ Уже в основном списке. Другой логин или /cancel")
                return
            if where == "user":
                bot.reply_to(message, "❌ Уже в разделе пользователей. Другой логин или /cancel")
                return
            add_state[uid] = {"step": "password", "login": login}
            bot.reply_to(message, "Теперь отправь <b>пароль</b>:")
            return
        if step == "password":
            add_state[uid] = {"step": "games", "login": st.get("login"), "password": text}
            bot.reply_to(message, "Теперь отправь список <b>игр</b> на аккаунте:")
            return
        if step == "games":
            login = st.get("login") or ""
            password = st.get("password") or ""
            games = text
            if len(games) < 2:
                bot.reply_to(message, "Укажи игры текстом. Ещё раз:")
                return
            now = time.time()
            left = int(CHECK_COOLDOWN - (now - last_check.get(uid, 0)))
            if left > 0:
                bot.reply_to(message, f"⏳ Подожди {left} сек. перед проверкой, потом снова /add")
                add_state.pop(uid, None)
                return
            last_check[uid] = now
            uname = message.from_user.username or message.from_user.first_name or str(uid)
            add_state[uid] = {
                "step": "checking",
                "login": login,
                "password": password,
                "games": games,
                "by": uname,
                "await_captcha_add": False,
            }

            def after_ok(l, p):
                finish_add_account(message.chat.id, uid, l, p, games, uname)

            bot.reply_to(
                message,
                "Проверяю аккаунт на Steam перед добавлением...\n"
                "Если попросят капчу — пришлю картинку, ответь текстом.",
            )
            run_check(message.chat.id, uid, login, password, after_ok=after_ok)
            return

    # default: search main
    do_search(message, text, source="main")


@app.get("/")
def health():
    return {
        "ok": True,
        "accounts": len(ACCOUNTS),
        "user_accounts": len(load_user_accounts()),
        "webhook": bool(WEBHOOK_URL),
        "checked": len(load_status()),
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
print(f"Ready | main={len(ACCOUNTS)} user={len(load_user_accounts())}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
