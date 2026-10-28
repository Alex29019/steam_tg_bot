"""
Steam TG Bot — Web Service (webhook + Flask).
Start: gunicorn -b 0.0.0.0:$PORT bot:app
Env: BOT_TOKEN, WEBHOOK_URL
"""
from __future__ import annotations

import json
import os
import random
import time
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
    headers = {
        "User-Agent": UA,
        "Referer": "https://steamcommunity.com/login/home/?goto=",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://steamcommunity.com",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        r = requests.get(
            "https://steamcommunity.com/login/getrsakey",
            params={"username": username, "donotcache": int(time.time() * 1000)},
            headers=headers,
            timeout=15,
        )
        rsa = r.json()
    except Exception:
        return {"status": "error", "message": "Ошибка сети (RSA)"}

    if not rsa or not rsa.get("success"):
        return {"status": "error", "message": "Аккаунт не работает"}

    try:
        enc_pass = encrypt_password(password, rsa["publickey_mod"], rsa["publickey_exp"])
    except Exception:
        return {"status": "error", "message": "Аккаунт не работает"}

    payload = {
        "username": username,
        "password": enc_pass,
        "twofactorcode": "",
        "emailauth": "",
        "loginfriendlyname": "",
        "captchagid": captcha_gid or "-1",
        "captcha_text": captcha_text or "",
        "emailsteamid": "",
        "rsatimestamp": rsa["timestamp"],
        "remember_login": "false",
        "donotcache": str(int(time.time() * 1000)),
    }
    try:
        r = requests.post(
            "https://steamcommunity.com/login/dologin/",
            data=payload,
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=15,
        )
        data = r.json()
    except Exception:
        return {"status": "error", "message": "Ошибка сети (login)"}

    if isinstance(data, str):
        return {"status": "error", "message": "Steam лимит — подождите 1–2 мин"}
    if not isinstance(data, dict):
        return {"status": "error", "message": "Аккаунт не работает"}
    if data.get("success") is True:
        return {"status": "success", "message": "Аккаунт работает"}
    if data.get("requires_twofactor") or data.get("emailauth_needed") or data.get("emailsteamid"):
        return {"status": "success", "message": "Аккаунт работает (Steam Guard / 2FA)"}
    if data.get("captcha_needed"):
        return {"status": "captcha", "message": "Требуется капча", "captcha_gid": str(data.get("captcha_gid") or "-1")}
    msg = str(data.get("message") or data.get("strError") or "").lower()
    if "too many" in msg or "rate" in msg:
        return {"status": "error", "message": "Steam лимит — подождите 1–2 мин"}
    return {"status": "error", "message": "Аккаунт не работает"}

# ===== end steam check =====

ROOT = Path(__file__).resolve().parent
ACCOUNTS = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
PAGE_SIZE = 8
CHECK_COOLDOWN = 60

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN not set")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

user_results: dict[int, list] = {}
last_check: dict[int, float] = {}
# uid -> {login, password, captcha_gid} waiting for captcha text
pending_captcha: dict[int, dict] = {}


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
            if len(out) >= 300:
                break
    return out


def page_kb(uid: int, page: int) -> types.InlineKeyboardMarkup:
    found = user_results.get(uid) or []
    pages = max(1, (len(found) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    kb = types.InlineKeyboardMarkup()
    start = page * PAGE_SIZE
    for i, a in enumerate(found[start : start + PAGE_SIZE]):
        idx = start + i
        games = a.get("g") or a.get("l") or "?"
        short = games if len(games) <= 40 else games[:37] + "..."
        kb.add(types.InlineKeyboardButton(f"{idx + 1}. {short}", callback_data=f"a:{idx}:{page}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("← Назад", callback_data=f"p:{page - 1}"))
    nav.append(types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(types.InlineKeyboardButton("Вперёд →", callback_data=f"p:{page + 1}"))
    if nav:
        kb.row(*nav)
    return kb


def acc_kb(idx: int, page: int, login: str, password: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    # copy_text (Bot API) — one tap copy; fallback handled in callback if needed
    try:
        kb.row(
            types.InlineKeyboardButton("📋 Логин", copy_text=types.CopyTextButton(text=login or "")),
            types.InlineKeyboardButton("📋 Пароль", copy_text=types.CopyTextButton(text=password or "")),
        )
    except Exception:
        kb.row(
            types.InlineKeyboardButton("📋 Логин", callback_data=f"cl:{idx}"),
            types.InlineKeyboardButton("📋 Пароль", callback_data=f"cp:{idx}"),
        )
    kb.add(types.InlineKeyboardButton("✅ Проверить", callback_data=f"c:{idx}"))
    kb.add(types.InlineKeyboardButton("« К списку", callback_data=f"p:{page}"))
    return kb


def fmt_acc(a: dict, n: int | None = None) -> str:
    head = f"<b>#{n}</b>\n" if n is not None else ""
    return (
        f"{head}🎮 <b>{esc(a.get('g') or '—')}</b>\n\n"
        f"👤 Логин:\n<code>{esc(a.get('l'))}</code>\n\n"
        f"🔑 Пароль:\n<code>{esc(a.get('p'))}</code>"
    )


def fetch_captcha_image(gid: str) -> bytes | None:
    try:
        r = requests.get(
            f"https://steamcommunity.com/login/rendercaptcha/?gid={gid}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:
        pass
    return None


def run_check(chat_id: int, uid: int, login: str, password: str, captcha_gid: str = "-1", captcha_text: str = ""):
    wait = bot.send_message(chat_id, "⏳ Проверка Steam...")
    result = check_steam(login, password, captcha_gid, captcha_text)
    status = result.get("status")
    msg = result.get("message") or status

    if status == "captcha":
        gid = str(result.get("captcha_gid") or "-1")
        pending_captcha[uid] = {"login": login, "password": password, "captcha_gid": gid}
        img = fetch_captcha_image(gid)
        text = (
            "⚠️ Steam просит <b>капчу</b>.\n"
            "Введи символы с картинки <b>следующим сообщением</b>."
        )
        try:
            bot.edit_message_text(text, chat_id, wait.message_id)
        except Exception:
            bot.send_message(chat_id, text)
        if img:
            bot.send_photo(chat_id, img, caption="Капча Steam — ответь текстом с картинки")
        else:
            bot.send_message(chat_id, f"Не удалось загрузить картинку. GID: <code>{esc(gid)}</code>")
        return

    pending_captcha.pop(uid, None)
    if status == "success":
        text = f"✅ <b>{esc(msg)}</b>\n👤 <code>{esc(login)}</code>"
    else:
        text = f"❌ <b>{esc(msg)}</b>\n👤 <code>{esc(login)}</code>"
    try:
        bot.edit_message_text(text, chat_id, wait.message_id)
    except Exception:
        bot.send_message(chat_id, text)


@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    bot.reply_to(
        message,
        f"Steam Accounts Bot\nВсего: <b>{len(ACCOUNTS)}</b>\n\n"
        f"Напиши игру для поиска или /find /random\n"
        f"Проверка — не чаще 1 раза в минуту.\n"
        f"Если Steam просит капчу — пришлём картинку, ответь текстом.",
    )


@bot.message_handler(commands=["random"])
def cmd_random(message: types.Message):
    a = random.choice(ACCOUNTS)
    user_results[message.from_user.id] = [a]
    bot.reply_to(
        message,
        fmt_acc(a, 1),
        reply_markup=acc_kb(0, 0, a.get("l") or "", a.get("p") or ""),
    )


@bot.message_handler(commands=["find"])
def cmd_find(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    do_search(message, parts[1].strip() if len(parts) > 1 else "")


def do_search(message: types.Message, q: str):
    if len(q) < 2:
        bot.reply_to(message, "Минимум 2 символа. Пример: <code>forza</code>")
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


@bot.callback_query_handler(func=lambda c: True)
def on_callback(cq: types.CallbackQuery):
    data = cq.data or ""
    uid = cq.from_user.id
    found = user_results.get(uid) or []

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
        bot.send_message(
            cq.message.chat.id,
            fmt_acc(a, idx + 1),
            reply_markup=acc_kb(idx, page, a.get("l") or "", a.get("p") or ""),
        )
        bot.answer_callback_query(cq.id)
        return

    # fallback copy buttons
    if data.startswith("cl:") or data.startswith("cp:"):
        try:
            idx = int(data.split(":")[1])
        except Exception:
            bot.answer_callback_query(cq.id)
            return
        if idx < 0 or idx >= len(found):
            bot.answer_callback_query(cq.id, "Сделай поиск снова", show_alert=True)
            return
        a = found[idx]
        val = a.get("l") if data.startswith("cl:") else a.get("p")
        bot.send_message(cq.message.chat.id, f"<code>{esc(val)}</code>")
        bot.answer_callback_query(cq.id, "Скопируй из сообщения")
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

    # If waiting captcha — this message is captcha solution
    if uid in pending_captcha:
        pend = pending_captcha.pop(uid)
        run_check(
            message.chat.id,
            uid,
            pend.get("login") or "",
            pend.get("password") or "",
            pend.get("captcha_gid") or "-1",
            text,
        )
        return

    do_search(message, text)


@app.get("/")
def health():
    return {"ok": True, "accounts": len(ACCOUNTS), "webhook": bool(WEBHOOK_URL)}, 200


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
print(f"Web service ready | accounts: {len(ACCOUNTS)} | PORT={PORT}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
