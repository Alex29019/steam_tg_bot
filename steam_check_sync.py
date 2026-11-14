"""Steam check without user captcha.

Uses IAuthenticationService first (no captcha_gid), then dologin.
If Steam forces captcha on this IP, returns clear error (not success).
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


UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
)


def _session(ua: str = UA_DESKTOP) -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(
        {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def _get_rsa(session: requests.Session, username: str) -> dict[str, Any] | None:
    # community
    try:
        r = session.get(
            "https://steamcommunity.com/login/getrsakey",
            params={"username": username, "donotcache": int(time.time() * 1000)},
            headers={
                "Referer": "https://steamcommunity.com/login/home/?goto=",
                "Origin": "https://steamcommunity.com",
            },
            timeout=20,
        )
        data = r.json()
        if data and data.get("success"):
            return data
    except Exception:
        pass
    # API
    try:
        r = session.get(
            "https://api.steampowered.com/IAuthenticationService/GetPasswordRSAPublicKey/v1/",
            params={"account_name": username},
            timeout=20,
        )
        data = r.json()
        resp = (data.get("response") if isinstance(data, dict) else None) or data
        if isinstance(resp, dict) and resp.get("publickey_mod") and resp.get("publickey_exp"):
            return {
                "success": True,
                "publickey_mod": resp["publickey_mod"],
                "publickey_exp": resp["publickey_exp"],
                "timestamp": str(resp.get("timestamp") or int(time.time())),
            }
    except Exception:
        pass
    return None


def _auth_service(session: requests.Session, username: str, password: str, rsa: dict) -> dict[str, Any]:
    try:
        enc = encrypt_password(password, rsa["publickey_mod"], rsa["publickey_exp"])
    except Exception:
        return {"status": "error", "message": "Аккаунт не работает"}

    # try form fields (no protobuf)
    form = {
        "account_name": username,
        "encrypted_password": enc,
        "encryption_timestamp": str(rsa["timestamp"]),
        "remember_login": "0",
        "persistence": "1",
        "website_id": "Community",
        "device_friendly_name": "Chrome",
        "platform_type": "2",
        "guard_data": "",
    }
    headers = {
        "Origin": "https://steamcommunity.com",
        "Referer": "https://steamcommunity.com/",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    url = "https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1/"

    for body in (
        form,
        {"input_json": json.dumps({
            "account_name": username,
            "encrypted_password": enc,
            "encryption_timestamp": str(rsa["timestamp"]),
            "remember_login": False,
            "platform_type": 2,
            "persistence": 1,
            "website_id": "Community",
            "device_details": {"device_friendly_name": "Chrome", "platform_type": 2},
        })},
    ):
        try:
            r = session.post(url, data=body, headers=headers, timeout=20)
            data = r.json() if r.content else {}
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        # Steam sometimes returns eresult at top level
        eresult = data.get("eresult")
        resp = data.get("response")
        if resp is None and isinstance(data.get("client_id"), (str, int)):
            resp = data
        if not isinstance(resp, dict):
            resp = {}

        if eresult is None:
            eresult = resp.get("eresult")

        # 1 = OK
        if resp.get("client_id") and (resp.get("request_id") or resp.get("steamid")):
            conf = resp.get("allowed_confirmations") or []
            # has confirmations → guard
            if conf:
                return {"status": "error", "message": "Не работает (Steam Guard / 2FA)"}
            # poll
            try:
                pr = session.post(
                    "https://api.steampowered.com/IAuthenticationService/PollAuthSessionStatus/v1/",
                    data={
                        "client_id": str(resp["client_id"]),
                        "request_id": str(resp.get("request_id") or ""),
                    },
                    headers=headers,
                    timeout=20,
                )
                pdata = pr.json().get("response") or {}
                if pdata.get("access_token") or pdata.get("refresh_token"):
                    return {"status": "success", "message": "Аккаунт работает"}
                if pdata.get("account_name"):
                    return {"status": "success", "message": "Аккаунт работает"}
            except Exception:
                pass
            # session began without explicit guard list
            return {"status": "success", "message": "Аккаунт работает"}

        # eresult codes: 5 invalid password, 84 rate limit, 101 failed, 63 login denied
        if eresult in (5, 8, 15, 101):
            return {"status": "error", "message": "Аккаунт не работает"}
        if eresult == 84:
            return {"status": "error", "message": "Steam лимит — подождите 1–2 мин"}

    return {"status": "error", "message": "auth_api_no_result"}


def _dologin(session: requests.Session, username: str, password: str, rsa: dict) -> dict[str, Any]:
    try:
        enc = encrypt_password(password, rsa["publickey_mod"], rsa["publickey_exp"])
    except Exception:
        return {"status": "error", "message": "Аккаунт не работает"}

    payload = {
        "donotcache": str(int(time.time() * 1000)),
        "username": username,
        "password": enc,
        "twofactorcode": "",
        "emailauth": "",
        "loginfriendlyname": "",
        "captchagid": "-1",
        "captcha_text": "",
        "emailsteamid": "",
        "rsatimestamp": rsa["timestamp"],
        "remember_login": "false",
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
            timeout=20,
        )
        data = r.json()
    except Exception as e:
        return {"status": "error", "message": f"Ошибка сети (login): {e}"}

    if isinstance(data, str):
        return {"status": "error", "message": "Steam лимит — подождите 1–2 мин"}
    if not isinstance(data, dict):
        return {"status": "error", "message": "Аккаунт не работает"}

    if data.get("success") is True:
        return {"status": "success", "message": "Аккаунт работает"}

    if data.get("requires_twofactor") or data.get("emailauth_needed") or data.get("emailsteamid"):
        return {"status": "error", "message": "Не работает (Steam Guard / 2FA)"}

    msg = str(data.get("message") or data.get("strError") or "")
    msg_l = msg.lower()

    if data.get("captcha_needed"):
        # sometimes still includes wrong-password text
        if any(x in msg_l for x in ("incorrect", "invalid", "password", "неверн")):
            return {"status": "error", "message": "Аккаунт не работает"}
        return {
            "status": "error",
            "message": "Steam блокирует IP (нужен другой IP/хостинг)",
        }

    if "incorrect" in msg_l or "invalid" in msg_l or "password" in msg_l:
        return {"status": "error", "message": "Аккаунт не работает"}
    if "too many" in msg_l or "rate" in msg_l:
        return {"status": "error", "message": "Steam лимит — подождите 1–2 мин"}

    return {"status": "error", "message": "Аккаунт не работает"}


def check_steam(
    username: str,
    password: str,
    captcha_gid: str = "-1",
    captcha_text: str = "",
    session: requests.Session | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    last = {"status": "error", "message": "Аккаунт не работает"}

    for ua in (UA_DESKTOP, UA_MOBILE):
        sess = session or _session(ua)
        try:
            sess.get(
                "https://steamcommunity.com/login/home/?goto=",
                timeout=15,
                headers={"Referer": "https://steamcommunity.com/"},
            )
        except Exception:
            pass

        rsa = _get_rsa(sess, username)
        if not rsa:
            last = {"status": "error", "message": "Ошибка сети (RSA)"}
            continue

        r1 = _auth_service(sess, username, password, rsa)
        if r1.get("status") == "success":
            return r1
        if r1.get("message") and "Guard" in r1["message"]:
            return r1
        if r1.get("message") and "лимит" in r1["message"]:
            return r1
        if r1.get("message") and r1["message"] != "auth_api_no_result":
            if "не работает" in (r1.get("message") or "").lower():
                # definitive bad password from API
                return r1

        r2 = _dologin(sess, username, password, rsa)
        if r2.get("status") == "success":
            return r2
        last = r2
        if "лимит" in (r2.get("message") or ""):
            return r2
        if "не работает" in (r2.get("message") or "").lower() and "IP" not in (r2.get("message") or ""):
            return r2

        # small pause before second UA
        time.sleep(0.4)

    return last
