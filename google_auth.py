"""Google Workspace OAuth -> Bright Data session (headless browser).

Flow:
  brightdata.com/users/auth/google -> accounts.google.com (identifier ->
  password -> workspace ToS/consent) -> callback back on Bright Data.

Uses CloakBrowser (Playwright-based stealth). This path is optional and only
required for Google Workspace (@yourdomain.com) accounts; GitHub auth needs no
browser at all. Requires the `cloakbrowser` package.
"""

from __future__ import annotations

import re

from common import BRD, log

BRD_GOOGLE_AUTH = "https://brightdata.com/users/auth/google"


async def google_auth(email: str, password: str, headless: bool = True) -> dict:
    """Log in via Google OAuth and return Bright Data session cookies."""
    from cloakbrowser import launch_async

    result: dict = {
        "email": email,
        "ok": False,
        "cookies": {},
        "cookie_header": "",
        "final_url": "",
        "error": None,
    }

    browser = await launch_async(headless=headless, humanize=True)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    page = await context.new_page()

    try:
        log("  [google] open Bright Data Google OAuth ...")
        await page.goto(BRD_GOOGLE_AUTH, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(1500)

        # --- identifier ---
        filled = False
        for sel in ['input[type="email"]', 'input[name="identifier"]', "#identifierId"]:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.fill(email)
                filled = True
                break
        if not filled and "accounts.google.com" in page.url:
            acc = page.locator(f'[data-email="{email}"], [data-identifier="{email}"]')
            if await acc.count() > 0:
                await acc.first.click()
                await page.wait_for_timeout(1500)
            else:
                other = page.get_by_text(re.compile("another account|Use another", re.I))
                if await other.count() > 0:
                    await other.first.click()
                    await page.wait_for_timeout(1000)
                    await page.locator('input[type="email"], #identifierId').first.fill(email)

        for sel in ['#identifierNext', 'button:has-text("Next")', 'button:has-text("Berikutnya")']:
            btn = page.locator(sel)
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click()
                break
        else:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(2000)

        # --- password ---
        pw_found = False
        for sel in ['input[type="password"]', 'input[name="Passwd"]', 'input[name="password"]']:
            loc = page.locator(sel)
            try:
                await loc.first.wait_for(state="visible", timeout=20000)
                await loc.first.fill(password)
                pw_found = True
                break
            except Exception:
                continue
        if not pw_found:
            result["error"] = f"password field not found url={page.url[:120]}"
            return result

        for sel in ['#passwordNext', 'button:has-text("Next")', 'button:has-text("Berikutnya")']:
            btn = page.locator(sel)
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click()
                break
        else:
            await page.keyboard.press("Enter")

        # --- wait for callback / handle workspace ToS, consent ---
        landed = False
        for tick in range(120):
            await page.wait_for_timeout(1000)
            u = page.url
            try:
                body = (await page.inner_text("body")).lower()
            except Exception:
                body = ""

            if (
                "workspacetermsofservice" in u
                or "speedbump" in u
                or "welcome to your new account" in body
                or "i understand" in body
            ):
                log("  [google] workspace ToS speedbump ...")
                for _ in range(8):
                    sb = page.locator('button[aria-label="Scroll down"], button[aria-label*="Scroll"]')
                    if await sb.count() > 0 and await sb.first.is_visible():
                        try:
                            await sb.first.click(timeout=1500)
                            await page.wait_for_timeout(400)
                        except Exception:
                            break
                    else:
                        try:
                            await page.mouse.wheel(0, 1200)
                            await page.wait_for_timeout(300)
                        except Exception:
                            pass
                        break
                clicked = False
                for sel in [
                    "#gaplustosNext button",
                    "#gaplustosNext",
                    'button:has-text("I understand")',
                    'button:has-text("I Understand")',
                ]:
                    b = page.locator(sel)
                    if await b.count() > 0:
                        try:
                            await b.first.click(timeout=3000, force=True)
                            clicked = True
                            await page.wait_for_timeout(2000)
                            break
                        except Exception:
                            pass
                if not clicked:
                    try:
                        await page.evaluate(
                            """() => {
                              const b = document.querySelector('#gaplustosNext button')
                                || [...document.querySelectorAll('button')].find(x => /i understand/i.test(x.innerText||''));
                              if (!b) return false;
                              b.click();
                              return true;
                            }"""
                        )
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass
                continue

            if "consent" in u or "/signin/oauth" in u or "oauth/consent" in u:
                for label in [
                    re.compile(r"^(Continue|Allow|Accept|Setuju|Lanjutkan|Izinkan)$", re.I),
                    re.compile(r"Continue|Allow|Accept", re.I),
                ]:
                    b = page.get_by_role("button", name=label)
                    if await b.count() > 0:
                        try:
                            await b.first.click(timeout=2000)
                            await page.wait_for_timeout(1500)
                            break
                        except Exception:
                            pass
                continue

            if any(
                x in body
                for x in [
                    "2-step", "2 step", "verify it’s you", "verify it's you",
                    "verification code", "phone number", "authenticator",
                ]
            ):
                result["error"] = "google_2fa_or_challenge"
                result["final_url"] = u
                return result

            if any(
                x in body
                for x in ["rejected", "access blocked", "couldn’t sign you in", "couldn't sign you in"]
            ):
                result["error"] = f"google_blocked: {body[:120]}"
                result["final_url"] = u
                return result

            if "brightdata.com" in u and "accounts.google.com" not in u:
                await page.wait_for_timeout(3000)
                names = {c["name"] for c in await context.cookies()}
                if "connect.sid" in names or "/cp" in page.url:
                    landed = True
                    break

            if tick in (15, 40, 70, 100):
                log(f"  [google] waiting ... {u[:90]}")

        result["final_url"] = page.url
        cookies = await context.cookies()
        jar = {c["name"]: c["value"] for c in cookies}
        result["cookies"] = jar
        result["cookie_header"] = "; ".join(f"{k}={v}" for k, v in jar.items())

        if landed and "connect.sid" in jar:
            result["ok"] = True
            log(f"  [google] OK landed {page.url[:90]}")
        else:
            result["error"] = result.get("error") or f"stuck url={page.url[:140]}"
            log(f"  [google] FAIL {result['error']}")

        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        log(f"  [google] EXC {result['error']}")
        return result
    finally:
        try:
            await browser.close()
        except Exception:
            pass
