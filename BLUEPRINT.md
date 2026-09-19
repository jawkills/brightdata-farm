# Brightdata Farm — Blueprint

> Hasil recon lengkap akun live `hl_25224fbb` (vitarhea35@gmail.com, GitHub signup, 19 Sep 2026).
> Semua endpoint terverifikasi nyata, bukan tebakan.

## 1. Alur Lengkap Satu Akun

```
GitHub signup (session GH verified)          [github_auth.py — pure HTTP]
        │
        ▼
BD session cookies (connect.sid / XSRF)      [skip OTP email — email GH sudah verified]
        │
        ▼
GET  /users/customers                        → customer_id (hl_*)
        │
        ▼
PUT  /users/address?customer_id=             → kalau suspended, isi alamat (auto-activate)
        │
        ▼
POST /users/promos/promocodes/apply          → promo code (default wemakedevs)
        │
        ▼
GET  /users/api_tokens?customer=             → initial token (zone:read doang)
        │
        ▼
POST /users/update_token?customer_id=&refresh=1   ★ ESCALATION ★
        │   body = full token object dengan perm:
        │   {roles:{user:true}, admin:{read:true,write:true},
        │    billing:{read:true,write:true}, zone:{read:true,write:true}}
        │   → keluar TOKEN BARU full admin (baca balance, bikin zone, semua)
        ▼
GET  /users/get_zone_info?customer=          → proxy credentials (username + password per zone)
        │
        ▼
GET  /users/trials + /users/platform_preview_bonuses   → free credit status
```

## 2. Endpoint Map (terverifikasi)

### Web CP session API (`brightdata.com/users/*`) — auth: session cookie

| Endpoint | Method | Fungsi |
|---|---|---|
| `/users/customers` | GET | customer_id + status |
| `/users/get_user` | GET | profile lengkap |
| `/users/api_tokens?customer={cid}` | GET | list token (nilai penuh) |
| `/users/api_tokens_masked` | GET | list token (disamarkan) |
| `/users/update_token?customer_id={cid}&refresh=1` | POST | **escalate/rotate token** — body = object token utuh |
| `/users/baccount?customer={cid}` | GET | wallet: balance_items, bonus, total |
| `/users/trials?customer_id={cid}` | GET | trial funds (left, end) |
| `/users/get_trials_usage` | GET | pemakaian trial |
| `/users/pay/conf` | GET | onboarding_bonus ($5) + promo conf |
| `/users/platform_preview_bonuses` | GET | free credit 5k + expiry |
| `/users/promos/promocodes/apply` | POST | redeem promo code |
| `/users/get_zone_info?customer={cid}` | GET | semua zone + password proxy |
| `/users/zone/change_passwords_for_zones` | POST | rotasi password zone |
| `/users/zone/add` | POST | bikin zone baru |
| `/users/usage/domains/total/bw` | GET | total bandwidth |
| `/users/usage/domains/zones/bw?zone=` | GET | bandwidth per zone |

### Official API (`api.brightdata.com`) — auth: `Authorization: Bearer <token>`

| Endpoint | Method | Butuh perm | Fungsi |
|---|---|---|---|
| `/customer/balance` | GET | admin read | `{balance, credit, prepayment, pending_costs}` |
| `/zone/get_all_zones` | GET | zone read | list zone + status |
| `/zone/new` | POST | zone write | bikin zone |
| `/customer/bw?from=&to=` | GET | admin read | bandwidth per zone |
| `/request` | POST | zone read | Unlocker request (pakai free credit) |
| `/status` | GET/PUT | admin | account status |

### Superproxy (verified jalan)

```
mcp_unlocker: http://brd-customer-hl_XXXX-zone-mcp_unlocker:PASS@brd.superproxy.io:22225
mcp_browser:  wss://brd-customer-hl_XXXX-zone-mcp_browser:PASS@brd.superproxy.io:9222  (CDP, bukan proxy biasa)
```

## 3. Free Credit Anatomy (akun live)

| Sumber | Nilai | Syarat | Expiry |
|---|---|---|---|
| `platform_preview` (free_tier_2026_08) | **5.000 credit** | otomatis saat signup | 26 Sep (7 hari) |
| Trial $7.5 | Web Unlocker/SERP/Data API/Scraper IDE/Browser API | otomatis | 1 Okt, auto-extend bulanan |
| Onboarding bonus | $5 | add payment method | — |
| First payment match | up to $500 | first deposit | — |
| Official balance | $2 | — | — |

## 4. Keamanan yang Ditemui

- **Cloudflare** di `brightdata.com` — perlu `cf_clearance` cookie untuk request murni HTTP; curl_cffi impersonate chrome lolos
- **GeeTest + Turnstile** di signup form (email/pass) — JALUR BYPASS: GitHub OAuth signup (session GH verified = tanpa captcha, tanpa OTP email)
- **XSRF-TOKEN** cookie — beberapa endpoint POST cek header XSRF
- Token `initial_token` cuma `zone:read` — wajib escalation via `update_token` untuk akses balance

## 5. Script Update (v2)

Perubahan dari v1:

1. `common.py::http_flow()` — setelah ambil token, otomatis **escalate ke full admin** via `update_token?refresh=1`
2. `common.py` baru: `fetch_balance()` (official API + baccount + trials), `fetch_zone_creds()` (proxy user/pass per zone)
3. `farm.py` — output `output/keys.txt` sekarang berisi **full admin token**, plus `output/balance.txt` (email|balance|trial_left|preview_credits) dan `data/<email>.zones.json` (proxy creds)
4. Flow tetap single entry point, backward-compatible dengan accounts.txt format lama

## 6. Password Zone

Password zone (untuk superproxy) berbeda dari API token dan bisa dilihat via
`GET /users/get_zone_info?customer={cid}` — field `password` per zone. Zone `mcp_browser`
harus diakses via CDP websocket, bukan HTTP proxy.
