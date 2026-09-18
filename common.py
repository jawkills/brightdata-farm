"""Shared helpers for Bright Data farming.

Contains the post-login HTTP flow (customer_id -> activate -> apply promo ->
fetch API key), TOTP generation, account-file parsing, and logging. Imported by
farm.py and both auth backends.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

BRD = "https://brightdata.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

# Promo code is configurable via env var; falls back to the default.
PROMO_CODE = os.environ.get("BRD_PROMO_CODE", "wemakedevs")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# TOTP (RFC 6238) — 6-digit OTP from base32 secret
# --------------------------------------------------------------------------- #
def totp_now(secret: str) -> str:
    """Return the current 6-digit TOTP for a base32 secret."""
    secret = secret.replace(" ", "").upper()
    secret += "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret)
    counter = int(time.time()) // 30
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def load_accounts(path: Path) -> list[list[str]]:
    """Parse an accounts file.

    Accepted line formats (delimiter `|`):
        email|password                    (no 2FA)
        email|password|totp_secret        (GitHub TOTP)
    Blank lines and lines starting with `#` are skipped.
    """
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            rows.append(parts)
    return rows


# --------------------------------------------------------------------------- #
# Post-login HTTP flow (shared by all auth backends)
# --------------------------------------------------------------------------- #
def http_flow(email: str, cookies: dict) -> dict:
    """Run the Bright Data billing flow once authenticated.

    Args:
        email: account identifier (used only for logging/storage).
        cookies: name->value dict of session cookies (must include connect.sid).

    Returns a dict with customer_id, api_token, promo_applied, and status info.
    """
    from curl_cffi import requests as R

    s = R.Session(impersonate="chrome")
    for k, v in cookies.items():
        try:
            s.cookies.set(k, v, domain=".brightdata.com")
        except Exception:
            pass
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json",
        "Origin": BRD,
        "Referer": f"{BRD}/cp/start",
        "x-requested-with": "XMLHttpRequest",
        "Cookie": cookie_header,
    }

    out: dict = {
        "customer_id": None,
        "api_token": None,
        "promo_applied": False,
        "error": None,
    }

    # 1) customer id
    try:
        r = s.get(f"{BRD}/users/customers", headers=headers, timeout=30)
        out["customers_status"] = r.status_code
        data = r.json()
        if isinstance(data, list) and data:
            out["customer_id"] = data[0].get("id") or data[0].get("account_id")
            out["customer_status"] = data[0].get("status")
        log(
            f"  [http] /customers {r.status_code} "
            f"cid={out['customer_id']} status={out.get('customer_status')}"
        )
    except Exception as e:
        out["error"] = f"customers: {e}"
        return out

    cid = out["customer_id"]
    if not cid:
        out["error"] = "no customer_id"
        return out

    # 2) activate if suspended (fill billing address)
    if out.get("customer_status") == "suspended":
        log("  [http] suspended -> fill billing address ...")
        try:
            r = s.put(
                f"{BRD}/users/address?customer_id={cid}",
                headers={**headers, "Referer": f"{BRD}/cp/billing/overview"},
                json={
                    "country": "ID",
                    "city": "Bandung",
                    "postal_code": "40232",
                    "line1": "Bandung",
                },
                timeout=30,
            )
            out["address_status"] = r.status_code
            log(f"  [http] PUT address {r.status_code}")
        except Exception as e:
            log(f"  [http] address warn: {e}")

    # 3) apply promo
    try:
        r = s.post(
            f"{BRD}/users/promos/promocodes/apply"
            f"?customer_id={cid}&promocode_id={PROMO_CODE}&action=change_plan",
            headers={**headers, "Referer": f"{BRD}/cp/billing/overview", "Content-Length": "0"},
            data=b"",
            timeout=30,
        )
        out["promo_status"] = r.status_code
        text = r.text or ""
        if r.status_code == 200 or "already activated" in text.lower():
            out["promo_applied"] = True
            if "already activated" in text.lower():
                out["promo_already"] = True
            log(f"  [http] promo {PROMO_CODE} ok (status {r.status_code})")
        else:
            log(f"  [http] promo status {r.status_code}: {text[:200]}")
    except Exception as e:
        log(f"  [http] promo exc: {e}")

    # 4) get api token
    try:
        r = s.get(f"{BRD}/users/api_tokens?customer_id={cid}", headers=headers, timeout=30)
        out["tokens_status"] = r.status_code
        data = r.json()
        if isinstance(data, list) and data and data[0].get("token"):
            out["api_token"] = data[0]["token"]
            out["token_email"] = data[0].get("email")
            out["token_expires"] = data[0].get("expires_at")
            log(f"  [http] API token OK {out['api_token'][:28]}...")
        else:
            out["tokens_body"] = data
            log(f"  [http] token body: {str(data)[:200]}")
    except Exception as e:
        out["error"] = out.get("error") or f"api_tokens: {e}"
        log(f"  [http] token exc: {e}")

    return out
