"""Shared helpers for Bright Data farming (v2).

Contains the post-login HTTP flow (customer_id -> activate -> apply promo ->
fetch API key -> escalate token to full admin -> fetch balance/zone creds),
TOTP generation, account-file parsing, and logging. Imported by farm.py and
both auth backends.
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
API = "https://api.brightdata.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

# Promo code is configurable via env var; falls back to the default.
PROMO_CODE = os.environ.get("BRD_PROMO_CODE", "wemakedevs")

# Full-admin permission set for token escalation (verified live 2026-09-19).
PERM_FULL = {
    "roles": {"user": True},
    "admin": {"read": True, "write": True},
    "billing": {"read": True, "write": True},
    "zone": {"read": True, "write": True},
}


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
# Session helpers
# --------------------------------------------------------------------------- #
def _session(cookies: dict):
    """Build a curl_cffi session impersonating chrome with BD cookies set."""
    from curl_cffi import requests as R

    s = R.Session(impersonate="chrome")
    for k, v in cookies.items():
        try:
            s.cookies.set(k, v, domain=".brightdata.com")
        except Exception:
            pass
    return s, "; ".join(f"{k}={v}" for k, v in cookies.items())


def _headers(cookie_header: str, referer: str = f"{BRD}/cp/start") -> dict:
    return {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json",
        "Origin": BRD,
        "Referer": referer,
        "x-requested-with": "XMLHttpRequest",
        "Cookie": cookie_header,
    }


# --------------------------------------------------------------------------- #
# Post-login HTTP flow (shared by all auth backends)
# --------------------------------------------------------------------------- #
def http_flow(email: str, cookies: dict) -> dict:
    """Run the Bright Data billing flow once authenticated.

    Steps: customer_id -> activate if suspended -> apply promo -> fetch API
    token -> escalate to full admin -> fetch balance + zone credentials.

    Args:
        email: account identifier (used only for logging/storage).
        cookies: name->value dict of session cookies (must include connect.sid).

    Returns a dict with customer_id, api_token (full admin), promo_applied,
    balance, zones, and status info.
    """
    out: dict = {
        "customer_id": None,
        "api_token": None,
        "promo_applied": False,
        "error": None,
    }

    s, cookie_header = _session(cookies)
    headers = _headers(cookie_header)

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
                headers=_headers(cookie_header, f"{BRD}/cp/billing/overview"),
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
            headers=_headers(cookie_header, f"{BRD}/cp/billing/overview") | {"Content-Length": "0"},
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

    # 4) get initial api token
    try:
        r = s.get(f"{BRD}/users/api_tokens?customer={cid}", headers=headers, timeout=30)
        out["tokens_status"] = r.status_code
        data = r.json()
        if isinstance(data, list) and data and data[0].get("token"):
            out["api_token"] = data[0]["token"]
            out["token_email"] = data[0].get("email")
            out["token_expires"] = data[0].get("expires_at")
            log(f"  [http] initial token OK {out['api_token'][:28]}...")
        else:
            out["tokens_body"] = data
            log(f"  [http] token body: {str(data)[:200]}")
    except Exception as e:
        out["error"] = out.get("error") or f"api_tokens: {e}"
        log(f"  [http] token exc: {e}")

    # 5) escalate token to full admin (verified live: POST update_token refresh=1)
    if out.get("api_token"):
        token_obj = {
            "token": out["api_token"],
            "email": out.get("token_email") or email,
            "expires_at": out.get("token_expires"),
            "perm": PERM_FULL,
        }
        try:
            r = s.post(
                f"{BRD}/users/update_token?customer_id={cid}&refresh=1",
                headers=_headers(cookie_header, f"{BRD}/cp/account_settings/api_tokens"),
                json=token_obj,
                timeout=30,
            )
            if r.status_code == 200:
                new = r.json()
                if new.get("token"):
                    out["api_token"] = new["token"]
                    out["token_escalated"] = True
                    log(f"  [http] token ESCALATED -> full admin {new['token'][:28]}...")
            else:
                out["escalate_status"] = r.status_code
                log(f"  [http] escalate status {r.status_code}: {r.text[:150]}")
        except Exception as e:
            log(f"  [http] escalate exc: {e}")

    # 6) fetch balance + zone creds with the (hopefully full) token
    tok = out.get("api_token")
    if tok:
        out["balance"] = fetch_balance(cid, tok)
        out["zones"] = fetch_zone_creds(cid, cookies)

    return out


# --------------------------------------------------------------------------- #
# Balance / credit / trial queries
# --------------------------------------------------------------------------- #
def fetch_balance(cid: str, api_token: str) -> dict:
    """Fetch balance from official API + trial/bonus from web CP.

    Returns dict: {balance, credit, prepayment, pending_costs, trial_left,
    trial_end, preview_credits, preview_expires}.
    """
    from curl_cffi import requests as R

    out: dict = {}
    h = {"Authorization": f"Bearer {api_token}"}
    try:
        r = R.get(f"{API}/customer/balance", headers=h, timeout=30, impersonate="chrome")
        if r.status_code == 200:
            out.update(r.json())
    except Exception as e:
        out["balance_err"] = str(e)
    return out


def fetch_balance_full(cid: str, api_token: str, cookies: dict) -> dict:
    """Official balance + web-CP trial/bonus detail (needs session cookies)."""
    out = fetch_balance(cid, api_token)

    s, cookie_header = _session(cookies)
    headers = _headers(cookie_header, f"{BRD}/cp/billing/overview")
    try:
        r = s.get(f"{BRD}/users/trials?customer_id={cid}", headers=headers, timeout=30)
        if r.status_code == 200:
            out["trials"] = r.json()
    except Exception as e:
        out["trials_err"] = str(e)
    try:
        r = s.get(f"{BRD}/users/platform_preview_bonuses", headers=headers, timeout=30)
        if r.status_code == 200:
            out["platform_preview"] = r.json()
    except Exception as e:
        out["preview_err"] = str(e)
    return out


# --------------------------------------------------------------------------- #
# Zone credentials (superproxy)
# --------------------------------------------------------------------------- #
def fetch_zone_creds(cid: str, cookies: dict) -> list[dict]:
    """Fetch all zones with proxy username/password via web CP.

    Returns list of {name, type, username, password, proxy_host, cdp_host}.
    """
    s, cookie_header = _session(cookies)
    headers = _headers(cookie_header, f"{BRD}/cp/zones")
    zones: list[dict] = []
    try:
        r = s.get(f"{BRD}/users/get_zone_info?customer={cid}", headers=headers, timeout=30)
        if r.status_code != 200:
            log(f"  [zones] get_zone_info {r.status_code}: {r.text[:120]}")
            return zones
        data = r.json()
        items = data if isinstance(data, list) else data.get("zones", [])
        for z in items:
            name = z.get("name", "")
            ztype = z.get("type", "")
            password = z.get("password") or z.get("passwd") or ""
            entry = {
                "name": name,
                "type": ztype,
                "username": f"brd-customer-{cid}-zone-{name}",
                "password": password,
                "proxy_host": "brd.superproxy.io:22225",
                "cdp_host": "brd.superproxy.io:9222",
            }
            zones.append(entry)
        log(f"  [zones] {len(zones)} zones fetched")
    except Exception as e:
        log(f"  [zones] exc: {e}")
    return zones
