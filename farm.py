#!/usr/bin/env python3
"""Bright Data account + promo-code farmer (v2).

Logs into Bright Data via GitHub OAuth (default, pure HTTP) or Google Workspace
OAuth (headless browser), applies a promo code, fetches the API key, escalates
it to full admin, then collects balance/trial/zone-proxy credentials.

Single entry point — no duplicate logic. Auth-specific code lives in
`github_auth.py` and `google_auth.py`; shared billing flow in `common.py`.

Usage:
  # GitHub (pure HTTP, recommended)
  python farm.py --method github --accounts accounts.txt
  python farm.py --method github --email you@example.com --password pass --secret TOTPSECRET

  # Google Workspace (needs cloakbrowser + playwright)
  python farm.py --method google --email you@domain.com --password pass
  python farm.py --method google --accounts accounts.txt

accounts.txt line format (delimiter `|`):
  email|password                  (no 2FA)
  email|password|totp_secret      (GitHub 2FA)

Output:
  output/keys.txt        -> email|api_token  (append; token is FULL ADMIN)
  output/balance.txt     -> email|balance|credit|trial_left (append)
  data/<email>.flow.json -> per-account result (balance + zones included)
  data/<email>.cookies.json
  data/<email>.zones.json -> superproxy credentials per zone

Config:
  BRD_PROMO_CODE env var overrides the promo code (default: wemakedevs).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from common import log, load_accounts, http_flow

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "output"
DATA.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)


def save_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def append_key(email: str, token: str) -> None:
    with (OUT / "keys.txt").open("a", encoding="utf-8") as f:
        f.write(f"{email}|{token}\n")


def append_balance(email: str, balance: dict) -> None:
    bal = balance.get("balance", "?")
    credit = balance.get("credit", "?")
    with (OUT / "balance.txt").open("a", encoding="utf-8") as f:
        f.write(f"{email}|{bal}|{credit}\n")


def auth_for(method: str, email: str, password: str, secret: str, headless: bool):
    """Return {"ok", "cookies", ...} for the chosen method."""
    if method == "github":
        from github_auth import github_auth
        return github_auth(email, password, secret or "")
    if method == "google":
        from google_auth import google_auth
        return asyncio.run(google_auth(email, password, headless=headless))
    raise ValueError(f"unknown method: {method}")


def run_one(method: str, parts: list[str], headless: bool) -> dict:
    email = parts[0]
    password = parts[1] if len(parts) > 1 else ""
    secret = parts[2] if len(parts) > 2 else ""

    log(f"== {email} [{method}] ==")
    auth = auth_for(method, email, password, secret, headless)

    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "email": email,
        "method": method,
        "auth_ok": auth.get("ok"),
        "error": auth.get("error"),
    }

    save_json(DATA / f"{email}.cookies.json", auth.get("cookies", {}))

    if not auth.get("ok"):
        row["ok"] = False
        save_json(DATA / f"{email}.flow.json", row)
        log(f"  FAIL {email}: {auth.get('error')}")
        return row

    http = http_flow(email, auth.get("cookies", {}))
    row.update(http)
    row["ok"] = bool(http.get("api_token")) and http.get("promo_applied")

    save_json(DATA / f"{email}.flow.json", row)
    if http.get("zones"):
        save_json(DATA / f"{email}.zones.json", http["zones"])

    if row["ok"]:
        append_key(email, http["api_token"])
        if http.get("balance"):
            append_balance(email, http["balance"])
        esc = " ESCALATED" if http.get("token_escalated") else ""
        log(f"  OK {email} token={http['api_token'][:24]}...{esc}")
    else:
        log(
            f"  PARTIAL {email}: promo={http.get('promo_applied')} "
            f"token={bool(http.get('api_token'))} err={http.get('error')}"
        )

    return row


def main() -> None:
    ap = argparse.ArgumentParser(description="Bright Data account + promo-code farmer v2")
    ap.add_argument("--method", choices=["github", "google"], default="github",
                    help="auth backend (default: github)")
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--secret", help="GitHub TOTP base32 secret (optional)")
    ap.add_argument("--accounts", help="file: email|password[|secret] per line")
    ap.add_argument("--show", action="store_true", help="show browser (google only)")
    args = ap.parse_args()

    if args.accounts:
        accts = load_accounts(Path(args.accounts))
    elif args.email and args.password:
        accts = [[args.email, args.password] + ([args.secret] if args.secret else [])]
    else:
        ap.error("need --accounts or (--email + --password)")

    for parts in accts:
        try:
            run_one(args.method, parts, headless=not args.show)
        except Exception as e:
            log(f"  EXC {parts[0] if parts else '?'}: {e}")
        time.sleep(2)


if __name__ == "__main__":
    main()
