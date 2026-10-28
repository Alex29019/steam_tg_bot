"""Steam check using requests (sync)."""
from __future__ import annotations

import base64
import time
from typing import Any

import requests
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
