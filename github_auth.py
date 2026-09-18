"""GitHub OAuth -> Bright Data session (pure HTTP, no browser).

Flow (reverse-engineered from a HAR capture):
  1) GET  brightdata.com/users/auth/github      -> 302 -> github authorize
  2) GET  github.com/login/oauth/authorize      -> login page (authenticity_token)
  3) POST github.com/session                    -> username + password
  4) POST github.com/sessions/two-factor        -> TOTP (if 2FA enabled)
  5) GET  github.com/login/oauth/authorize      -> approve page token
  6) POST github.com/login/oauth/authorize      -> meta-refresh code
  7) GET  brightdata.com/users/auth/github/done?code=... -> connect.sid

Requires only `curl_cffi` — no browser needed.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs

from common import BRD, UA, log, totp_now

GH = "https://github.com"
GH_CLIENT_ID = "20ebc63053c9dd67597d"
BRD_REDIRECT = "https://brightdata.com/users/auth/github/done"


def _extract_token(html: str, name: str = "authenticity_token") -> str | None:
    m = re.search(rf'name="{re.escape(name)}"[^>]*value="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(rf'value="([^"]+)"[^>]*name="{re.escape(name)}"', html)
    return m.group(1) if m else None


def github_auth(email: str, password: str, secret: str = "") -> dict:
    """Log in via GitHub OAuth and return Bright Data session cookies.

    Args:
        email: GitHub username or email.
        password: GitHub password.
        secret: optional base32 TOTP secret for accounts with 2FA.

    Returns:
        {"ok": bool, "cookies": {...}, "connect_sid": str|None, "error": str|None}
    """
    from curl_cffi import requests as R

    s = R.Session(impersonate="chrome")
    s.headers.update({"User-Agent": UA, "Accept": "text/html,*/*"})

    out: dict = {"ok": False, "cookies": {}, "connect_sid": None, "error": None}

    # 1) brightdata -> github authorize redirect
    try:
        r = s.get(
            f"{BRD}/users/auth/github",
            params={
                "action": "signup",
                "signup_url": BRD + "/",
                "next": f"{BRD}/google_signup?type=github",
            },
            allow_redirects=False,
            timeout=30,
        )
        loc = r.headers.get("Location", "")
        log(f"  [gh] brightdata auth/github -> {r.status_code}")
        if "github.com" not in loc:
            out["error"] = f"no github redirect: {loc[:120]}"
            return out
        for cname in list(s.cookies):
            out["cookies"][cname] = s.cookies.get(cname)
        auth_url = loc
    except Exception as e:
        out["error"] = f"brightdata auth: {e}"
        return out

    # 2) login page -> authenticity_token
    try:
        r = s.get(auth_url, allow_redirects=True, timeout=30)
        login_html = r.text
        if "password" not in login_html:
            r = s.get(f"{GH}/login", allow_redirects=True, timeout=30)
            login_html = r.text
        token = _extract_token(login_html)
        if not token:
            out["error"] = "no login authenticity_token"
            return out
        log(f"  [gh] login page ok url={str(r.url)[:70]}")
    except Exception as e:
        out["error"] = f"login page: {e}"
        return out

    parsed = urlparse(auth_url)
    return_to = parsed.path + "?" + parsed.query

    # 3) POST /session
    try:
        r = s.post(
            f"{GH}/session",
            data={
                "commit": "Sign in",
                "authenticity_token": token,
                "login": email,
                "password": password,
                "webauthn-conditional": "undefined",
                "javascript-support": "true",
                "webauthn-support": "supported",
                "webauthn-iuvpaa-support": "unsupported",
                "return_to": return_to,
            },
            headers={
                "Origin": GH,
                "Referer": f"{GH}/login",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            allow_redirects=False,
            timeout=30,
        )
        loc = r.headers.get("Location", "")
        log(f"  [gh] /session -> {r.status_code}")
        if "two-factor" in loc:
            twofa_url = loc if loc.startswith("http") else GH + loc
        elif r.status_code == 302:
            twofa_url = None  # no 2FA
        else:
            out["error"] = f"login failed (status {r.status_code})"
            return out
    except Exception as e:
        out["error"] = f"session post: {e}"
        return out

    # 4) 2FA
    if twofa_url:
        if not secret:
            out["error"] = "github requires 2FA but no totp secret provided"
            return out
        try:
            r = s.get(twofa_url, allow_redirects=True, timeout=30)
            twofa_token = _extract_token(r.text)
            if not twofa_token:
                out["error"] = "no 2fa authenticity_token"
                return out
            otp = totp_now(secret)
            log(f"  [gh] 2FA OTP={otp}")
            r = s.post(
                f"{GH}/sessions/two-factor",
                data={"authenticity_token": twofa_token, "app_otp": otp},
                headers={
                    "Origin": GH,
                    "Referer": twofa_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                allow_redirects=False,
                timeout=30,
            )
            loc = r.headers.get("Location", "")
            if "two-factor" in loc:
                out["error"] = "2FA failed (bad otp?)"
                return out
            log(f"  [gh] 2FA -> {r.status_code}")
        except Exception as e:
            out["error"] = f"2fa: {e}"
            return out

    # 5) authorize page (auto-approve if already authorized)
    try:
        parsed = urlparse(auth_url)
        q = parse_qs(parsed.query)
        state = q.get("state", [""])[0]

        def _find_code(text: str, url: str) -> str | None:
            m = re.search(r"code=([a-zA-Z0-9]+)", text)
            if m:
                return m.group(1)
            m = re.search(r"code=([a-zA-Z0-9]+)", url)
            return m.group(1) if m else None

        # First attempt: if already authorized, GitHub 302s straight to the
        # redirect_uri with the code in the Location header (no approve page).
        r = s.get(auth_url, allow_redirects=False, timeout=30)
        code = _find_code(r.headers.get("Location", ""), "")
        if code:
            log("  [gh] already authorized, code from redirect")
        else:
            # Not auto-authorized: get the approve page and submit the form.
            r = s.get(auth_url, allow_redirects=True, timeout=30)
            code = _find_code(r.headers.get("Location", ""), str(r.url))
            if not code:
                approve_token = _extract_token(r.text)
                if not approve_token:
                    out["error"] = "no authorize authenticity_token and no code"
                    return out
                log("  [gh] authorize page ok")
                r = s.post(
                    f"{GH}/login/oauth/authorize",
                    data={
                        "authorize": "1",
                        "authenticity_token": approve_token,
                        "redirect_uri_specified": "true",
                        "client_id": GH_CLIENT_ID,
                        "redirect_uri": BRD_REDIRECT,
                        "state": state,
                        "scope": "user:email",
                    },
                    headers={
                        "Origin": GH,
                        "Referer": auth_url,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    allow_redirects=False,
                    timeout=30,
                )
                code = _find_code(r.headers.get("Location", ""), r.text)

        if not code:
            out["error"] = "no code from authorize"
            return out
        log(f"  [gh] authorize code={code[:16]}...")
    except Exception as e:
        out["error"] = f"authorize: {e}"
        return out

    # 7) brightdata done -> connect.sid
    try:
        done_url = f"{BRD_REDIRECT}?code={code}&iss={GH}/login/oauth&state={state}"
        r = s.get(done_url, allow_redirects=True, timeout=30)
        for cname in list(s.cookies):
            out["cookies"][cname] = s.cookies.get(cname)
        if "connect.sid" in out["cookies"]:
            out["connect_sid"] = out["cookies"]["connect.sid"]
            out["ok"] = True
            log(f"  [gh] session OK cookies={len(out['cookies'])}")
        else:
            out["error"] = f"no connect.sid (url={str(r.url)[:100]})"
    except Exception as e:
        out["error"] = f"done: {e}"

    return out
