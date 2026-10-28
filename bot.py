"""
Telegram-бот: поиск общих Steam-аккаунтов + проверка логина/пароля.
Запуск:
  1) pip install -r requirements.txt
  2) export BOT_TOKEN="123:ABC..."
  3) python bot.py
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.enums import ParseMode

from steam_check import check_steam

ROOT = Path(__file__).resolve().parent
ACCOUNTS = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
PAGE_SIZE = 5

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("Укажи токен: export BOT_TOKEN='...'")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# user_id -> last search results (list of dicts)
user_results: dict[int, list] = {}


def esc(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def search_accounts(query: str) -> list:
    q = (query or "").strip().lower()
    if not q:
        return []
    out = []
    for a in ACCOUNTS:
        if q in (a.get("l") or "").lower() or q in (a.get("g") or "").lower():
            out.append(a)
            if len(out) >= 200:
                break
    return out


def page_keyboard(uid: int, page: int, total: int) -> InlineKeyboardMarkup:
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    rows = []
    start = page * PAGE_SIZE
    chunk = user_results.get(uid, [])[start : start + PAGE_SIZE]
    for i, a in enumerate(chunk):
        idx = start + i
        short = (a.get("g") or a.get("l") or "?")[:32]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{idx + 1}. {short}",
                    callback_data=f"acc:{idx}",
                )
            ]
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"page:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_keyboard(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔑 Логин", callback_data=f"login:{idx}"),
                InlineKeyboardButton(text="🔐 Пароль", callback_data=f"pass:{idx}"),
            ],
            [
                InlineKeyboardButton(text="✅ Проверить", callback_data=f"check:{idx}"),
            ],
            [
                InlineKeyboardButton(text="« К списку", callback_data="page:0"),
            ],
        ]
    )


def format_account(a: dict, n: int | None = None) -> str:
    head = f"<b>#{n}</b>\n" if n is not None else ""
    return (
        f"{head}"
        f"🎮 <b>{esc(a.get('g') or '—')}</b>\n"
        f"👤 <code>{esc(a.get('l'))}</code>\n"
        f"🔑 <code>{esc(a.get('p'))}</code>"
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"Steam Accounts Bot\n"
        f"Всего аккаунтов: <b>{len(ACCOUNTS)}</b>\n\n"
        f"Команды:\n"
        f"/find <i>игра или логин</i> — поиск\n"
        f"/check <i>логин пароль</i> — проверка Steam\n"
        f"/random — случайный аккаунт\n"
        f"/help — помощь",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Примеры:\n"
        "<code>/find forza</code>\n"
        "<code>/find wukong</code>\n"
        "<code>/check login password</code>\n"
        "<code>/random</code>\n\n"
        "В карточке аккаунта:\n"
        "• Логин / Пароль — отдельным сообщением (удобно копировать)\n"
        "• Проверить — запрос к Steam",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("random"))
async def cmd_random(message: Message):
    import random

    a = random.choice(ACCOUNTS)
    user_results[message.from_user.id] = [a]
    await message.answer(
        format_account(a, 1),
        parse_mode=ParseMode.HTML,
        reply_markup=account_keyboard(0),
    )


@dp.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject):
    q = (command.args or "").strip()
    if len(q) < 2:
        await message.answer("Напиши запрос: <code>/find forza</code>", parse_mode=ParseMode.HTML)
        return
    found = search_accounts(q)
    if not found:
        await message.answer(f"Ничего не найдено по: <b>{esc(q)}</b>", parse_mode=ParseMode.HTML)
        return
    uid = message.from_user.id
    user_results[uid] = found
    await message.answer(
        f"Найдено: <b>{len(found)}</b> по «{esc(q)}»\nВыбери аккаунт:",
        parse_mode=ParseMode.HTML,
        reply_markup=page_keyboard(uid, 0, len(found)),
    )


@dp.message(Command("check"))
async def cmd_check(message: Message, command: CommandObject):
    args = (command.args or "").strip()
    parts = args.split(None, 1)
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/check логин пароль</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    login, password = parts[0], parts[1]
    wait = await message.answer("⏳ Проверяю Steam...")
    result = await check_steam(login, password)
    status = result.get("status")
    msg = result.get("message") or status
    if status == "success":
        text = f"✅ <b>{esc(msg)}</b>\n👤 <code>{esc(login)}</code>"
    elif status == "captcha":
        text = f"⚠️ {esc(msg)}\nПопробуй позже или другой аккаунт."
    else:
        text = f"❌ <b>{esc(msg)}</b>\n👤 <code>{esc(login)}</code>"
    await wait.edit_text(text, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery):
    await cq.answer()


@dp.callback_query(F.data.startswith("page:"))
async def cb_page(cq: CallbackQuery):
    uid = cq.from_user.id
    page = int(cq.data.split(":")[1])
    found = user_results.get(uid) or []
    if not found:
        await cq.answer("Сначала сделай /find", show_alert=True)
        return
    await cq.message.edit_reply_markup(reply_markup=page_keyboard(uid, page, len(found)))
    await cq.answer()


@dp.callback_query(F.data.startswith("acc:"))
async def cb_acc(cq: CallbackQuery):
    uid = cq.from_user.id
    idx = int(cq.data.split(":")[1])
    found = user_results.get(uid) or []
    if idx < 0 or idx >= len(found):
        await cq.answer("Нет такого", show_alert=True)
        return
    a = found[idx]
    await cq.message.answer(
        format_account(a, idx + 1),
        parse_mode=ParseMode.HTML,
        reply_markup=account_keyboard(idx),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("login:"))
async def cb_login(cq: CallbackQuery):
    uid = cq.from_user.id
    idx = int(cq.data.split(":")[1])
    found = user_results.get(uid) or []
    if idx >= len(found):
        await cq.answer("Нет", show_alert=True)
        return
    await cq.message.answer(f"<code>{esc(found[idx].get('l'))}</code>", parse_mode=ParseMode.HTML)
    await cq.answer("Логин")


@dp.callback_query(F.data.startswith("pass:"))
async def cb_pass(cq: CallbackQuery):
    uid = cq.from_user.id
    idx = int(cq.data.split(":")[1])
    found = user_results.get(uid) or []
    if idx >= len(found):
        await cq.answer("Нет", show_alert=True)
        return
    await cq.message.answer(f"<code>{esc(found[idx].get('p'))}</code>", parse_mode=ParseMode.HTML)
    await cq.answer("Пароль")


@dp.callback_query(F.data.startswith("check:"))
async def cb_check(cq: CallbackQuery):
    uid = cq.from_user.id
    idx = int(cq.data.split(":")[1])
    found = user_results.get(uid) or []
    if idx >= len(found):
        await cq.answer("Нет", show_alert=True)
        return
    a = found[idx]
    await cq.answer("Проверяю...")
    wait = await cq.message.answer("⏳ Проверка Steam...")
    result = await check_steam(a.get("l") or "", a.get("p") or "")
    status = result.get("status")
    msg = result.get("message") or status
    if status == "success":
        text = f"✅ <b>{esc(msg)}</b>\n👤 <code>{esc(a.get('l'))}</code>"
    elif status == "captcha":
        text = f"⚠️ {esc(msg)}"
    else:
        text = f"❌ <b>{esc(msg)}</b>"
    await wait.edit_text(text, parse_mode=ParseMode.HTML)


@dp.message(F.text)
async def fallback_search(message: Message):
    """Любой текст = поиск, если не команда."""
    q = (message.text or "").strip()
    if not q or q.startswith("/"):
        return
    if len(q) < 2:
        return
    found = search_accounts(q)
    if not found:
        await message.answer(f"Ничего не найдено по: <b>{esc(q)}</b>", parse_mode=ParseMode.HTML)
        return
    uid = message.from_user.id
    user_results[uid] = found
    await message.answer(
        f"Найдено: <b>{len(found)}</b>\nВыбери аккаунт:",
        parse_mode=ParseMode.HTML,
        reply_markup=page_keyboard(uid, 0, len(found)),
    )


async def main():
    print(f"Bot started | accounts: {len(ACCOUNTS)}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
