# Steam Accounts Telegram Bot

Тот же список аккаунтов + проверка Steam, но в Telegram.

## Установка

```bash
cd steam_tg_bot
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Токен

1. Открой @BotFather в Telegram  
2. `/newbot` → получи токен  
3. Запуск:

```bash
export BOT_TOKEN="123456:ABC-DEF..."
python bot.py
```

Windows PowerShell:
```powershell
$env:BOT_TOKEN="123456:ABC-DEF..."
python bot.py
```

## Команды

| Команда | Описание |
|---------|----------|
| `/start` | Старт |
| `/find forza` | Поиск по игре/логину |
| `/check login pass` | Проверка на Steam |
| `/random` | Случайный аккаунт |
| просто текст | тоже поиск |

В карточке аккаунта кнопки: **Логин**, **Пароль**, **Проверить**.

## Файлы

- `bot.py` — бот
- `steam_check.py` — проверка Steam (RSA)
- `accounts.json` — база аккаунтов (~2293)
- `requirements.txt` — зависимости
