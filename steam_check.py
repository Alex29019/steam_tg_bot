"""Async wrapper — runs captcha-free sync checker in thread."""
from __future__ import annotations

import asyncio
from typing import Any

from steam_check_sync import check_steam as check_steam_sync


async def check_steam(
    username: str,
    password: str,
    captcha_gid: str = "-1",
    captcha_text: str = "",
) -> dict[str, Any]:
    return await asyncio.to_thread(
        check_steam_sync, username, password, captcha_gid, captcha_text
    )
