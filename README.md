# brightdata-farm

Automate **Bright Data** account sign-in, promo-code application, and API-key
harvesting. Logs in via **GitHub OAuth** (pure HTTP, no browser) or **Google
Workspace OAuth** (headless browser), applies a promo code, and returns the API
key.

> ⚠️ **Use responsibly.** This automates account actions on `brightdata.com`.
> Only use it on accounts you own or are authorized to manage. Promo codes are
> bound to Bright Data's own eligibility rules and may not apply to every
> account.

## Features

- **GitHub auth** — pure HTTP, no browser, fast. Supports accounts with
  TOTP-based 2FA.
- **Google Workspace auth** — headless browser via CloakBrowser/Playwright.
- Applies a promo code (default `wemakedevs`, overridable via env).
- Fetches the API key (`initial_token`) and appends `email|token` to
  `output/keys.txt`.
- Per-account JSON state in `data/`.

## Requirements

- Python 3.10+
- `curl_cffi` (both methods)
- `cloakbrowser` + Playwright (Google method only — optional)

```bash
pip install -r requirements.txt
```

## Quick start

### GitHub (recommended — no browser)

```bash
# single account with 2FA
python farm.py --method github \
  --email you@example.com \
  --password 'yourpassword' \
  --secret 'BASE32TOTPSECRET'

# single account without 2FA
python farm.py --method github --email you@example.com --password 'yourpassword'

# bulk from a file
python farm.py --method github --accounts accounts.txt
```

### Google Workspace

```bash
python farm.py --method google --email you@yourdomain.com --password 'pass'
python farm.py --method google --accounts accounts.txt
# add --show to watch the browser
```

## accounts.txt format

One account per line, `|`-delimited:

```
# email | password | totp_secret (secret optional, GitHub only)
you@example.com|yourpassword
other@example.com|theirpassword|BASE32SECRET
```

Lines starting with `#` are ignored.

## Output

- `output/keys.txt` — harvested API keys, one `email|token` per line.
- `data/<email>.flow.json` — per-account result (customer id, promo status,
  token expiry).
- `data/<email>.cookies.json` — session cookies (ignored by git).

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `BRD_PROMO_CODE` | `wemakedevs` | Promo code to apply |

```bash
BRD_PROMO_CODE=othercode python farm.py --method github --accounts accounts.txt
```

## Project layout

```
farm.py          # single CLI entry point
common.py        # shared billing flow + TOTP + helpers
github_auth.py   # GitHub OAuth -> Bright Data session (pure HTTP)
google_auth.py   # Google Workspace OAuth -> Bright Data session (browser)
data/            # per-account state (git-ignored)
output/          # harvested keys (git-ignored)
```
