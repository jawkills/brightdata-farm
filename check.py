#!/usr/bin/env python3
"""Re-check balance + free credits for all harvested accounts.

Reads every data/<email>.cookies.json (saved by farm.py), re-runs the
balance/credit queries against the web CP + official API, and appends a
timestamped line to output/checks.txt.

Usage:
  python check.py                # all accounts with saved cookies
  python check.py --email a@b.c  # single account
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from common import (
    BRD, PERM_FULL, log, _session, _headers, fetch_balance,
)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "output"


def get_customer(cookies: dict) -> tuple[str | None, dict]:
    """Fetch customer_id + record via /users/customers with session cookies."""
    s, ch = _session(cookies)
    try:
        r = s.get(f"{BRD}/users/customers", headers=_headers(ch), timeout=30)
        data = r.json()
        if isinstance(data, list) and data:
            return data[0].get("id") or data[0].get("account_id"), data[0]
    except Exception as e:
        log(f"  customers exc: {e}")
    return None, {}


def rotate_token(cookies: dict, cid: str, email: str) -> str | None:
    """Get a fresh full-admin token via POST /users/update_token?refresh=1.

    The CP JS sends the token object with token:null to rotate; expires_at
    must be included (a future ISO date) or the API issues an already-expired
    token. The response carries the new token value.
    """
    s, ch = _session(cookies)
    expires = (datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year + 1)
               .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    body = {
        "token": None,
        "email": email,
        "expires_at": expires,
        "perm": PERM_FULL,
    }
    try:
        r = s.post(
            f"{BRD}/users/update_token?customer_id={cid}&refresh=1",
            headers=_headers(ch, f"{BRD}/cp/account_settings/api_tokens"),
            json=body,
            timeout=30,
        )
        if r.status_code == 200:
            new = r.json()
            return new.get("token")
        log(f"  rotate_token {r.status_code}: {r.text[:120]}")
    except Exception as e:
        log(f"  rotate_token exc: {e}")
    return None


def parse_trials(payload) -> list[dict]:
    """Normalize /users/trials payload (fields: trial_left, trial_used, ...)."""
    out = []
    try:
        items = payload if isinstance(payload, list) else [payload]
        for it in items:
            if isinstance(it, dict) and ("trial_left" in it or "is_active" in it):
                out.append({
                    "active": bool(it.get("is_active")),
                    "limit": it.get("trial_limit"),
                    "used": it.get("trial_used"),
                    "left": it.get("trial_left"),
                    "end": it.get("end"),
                    "products": it.get("products"),
                    "auto_extension": it.get("auto_extension"),
                })
    except Exception:
        pass
    return out


def parse_preview(payload) -> tuple[int | None, str | None]:
    """Extract (credits, expires) from platform_preview_bonuses payload."""
    try:
        items = payload if isinstance(payload, list) else [payload]
        for it in items:
            if not isinstance(it, dict):
                continue
            for credits_key in ("credits_left", "credits", "left", "amount", "value"):
                if it.get(credits_key) is not None:
                    expires = (
                        it.get("expires_at") or it.get("expires")
                        or it.get("end") or it.get("expiry")
                    )
                    return it[credits_key], expires
    except Exception:
        pass
    return None, None


def check_one(email: str) -> dict:
    log(f"== check {email} ==")
    ck_path = DATA / f"{email}.cookies.json"
    if not ck_path.exists():
        log(f"  no cookies for {email}")
        return {"email": email, "ok": False, "error": "no cookies"}

    cookies = json.loads(ck_path.read_text(encoding="utf-8"))
    if not cookies:
        return {"email": email, "ok": False, "error": "empty cookies"}

    row: dict = {
        "email": email,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    cid, cust = get_customer(cookies)
    row["customer_id"] = cid
    row["customer_status"] = cust.get("status")
    if not cid:
        row["ok"] = False
        row["error"] = "session dead (no customer_id) — re-farm needed"
        log(f"  SESSION DEAD {email}")
        return row

    # fresh full-admin token via rotation
    tok = rotate_token(cookies, cid, email)
    if tok:
        row["api_token"] = tok
        row["api_balance"] = fetch_balance(cid, tok)
        log(f"  token rotated, balance: {row['api_balance']}")
    else:
        row["api_balance"] = None
        log("  token rotation failed (balance via web only)")

    # web CP: baccount (balance items + auto credits) + trials
    s, ch = _session(cookies)
    headers = _headers(ch, f"{BRD}/cp/billing/overview")

    row["auto_credits"] = []
    row["balance_items"] = []
    try:
        r = s.get(f"{BRD}/users/baccount?customer={cid}", headers=headers, timeout=30)
        if r.status_code == 200:
            acc = r.json()
            row["pending_bonuses"] = acc.get("pending_bonuses")
            row["balance_items"] = acc.get("balance_items", [])
            for ac in (acc.get("auto_credits") or {}).values():
                row["auto_credits"].append({
                    "type": ac.get("type"),
                    "limit": ac.get("limit"),
                    "spent": ac.get("spent"),
                    "left": (ac.get("limit") or 0) - (ac.get("spent") or 0),
                    "expire_ts": ac.get("expire_ts"),
                })
        else:
            row["baccount_status"] = r.status_code
    except Exception as e:
        row["baccount_err"] = str(e)

    try:
        r = s.get(f"{BRD}/users/trials?customer_id={cid}", headers=headers, timeout=30)
        row["trials"] = parse_trials(r.json()) if r.status_code == 200 else []
    except Exception as e:
        row["trials_err"] = str(e)

    trial_left = next((t["left"] for t in row.get("trials", []) if t.get("active")), None)
    pv = next((a for a in row["auto_credits"] if a.get("type") == "platform_preview"), None)
    log(
        f"  trial_left={trial_left} preview_left={pv['left'] if pv else None} "
        f"preview_exp={pv['expire_ts'] if pv else None} "
        f"pending_bonuses={row.get('pending_bonuses')}"
    )
    row["ok"] = True
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-check balance + credits per account")
    ap.add_argument("--email", help="check a single account")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    if args.email:
        emails = [args.email]
    else:
        emails = sorted(
            p.name[:-len(".cookies.json")]
            for p in DATA.glob("*.cookies.json")
        )
    if not emails:
        ap.error("no accounts with saved cookies in data/")

    results = []
    for email in emails:
        try:
            r = check_one(email)
        except Exception as e:
            r = {"email": email, "ok": False, "error": str(e)}
        results.append(r)

        trial_left = next(
            (t.get("left") for t in (r.get("trials") or []) if t.get("active")), None
        )
        pv = next(
            (a.get("left") for a in (r.get("auto_credits") or [])
             if a.get("type") == "platform_preview"), None,
        )
        bal = (r.get("api_balance") or {}).get("balance", "?")
        with (OUT / "checks.txt").open("a", encoding="utf-8") as f:
            f.write(
                f"{r['ts']}|{email}|{'OK' if r.get('ok') else 'DEAD'}"
                f"|bal={bal}|trial_left={trial_left}"
                f"|preview_left={pv if pv is not None else '?'}"
                f"|pending_bonuses={r.get('pending_bonuses', '?')}\n"
            )

    ok = sum(1 for r in results if r.get("ok"))
    log(f"done: {ok}/{len(results)} alive -> output/checks.txt")


if __name__ == "__main__":
    main()
