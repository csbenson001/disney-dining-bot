# Magic Table Finder — Product and ops brief

> Agent law is `AGENTS.md`. This file is product/ops detail only. Do not launch Claude Code.

## What This Project Does
Self-hosted MouseWatcher-style service for Walt Disney World dining. A web UI at **magictablefinder.com** lets each user manage their own watch list; a VPS worker polls Disney and sends SMS alerts to the user who created the watch.

Built and maintained by Craig Owen for personal use (Craig + wife).

## Product Promise

The product promise is narrow and important:

> Craig and Jessica can create precise Disney dining watches from a simple UI, and the system only alerts the right person when a genuinely new matching opening appears.

Do not regress this into a generic availability dashboard. Alerts are for **newly opened matching reservation times**, not summaries of every currently available time.

## Consumer direction

Public launch is in progress. Tracking spec: [CONSUMER-EPIC.md](CONSUMER-EPIC.md). **Slice 4 (current):** global consumer watch budget + locked public hero line. Slice 3 (Stripe hybrid) is shipped: Single Watch Checkout (`mode=payment`) and Planner (`mode=subscription`). Webhooks (plus server Session retrieve) own entitlement. Unpaid consumer watches never go in `watches.json`. Craig and Jessica stay off Stripe. Do not put live `STRIPE_*` values in git or Cloud secrets. Do not put consumer accounts in `WATCH_USERS`. Do not clear `open_slots.json` or `seen_slots.json`.

**Billable watch (locked):** one restaurant + party + meal periods + optional time window + one or more dates. Never treat one `watches.json` date row as a paid alert.

**Identity (Slice 2):** Consumers sign in with an email magic link (`POST /_api/auth/magic-link`, `GET /_api/auth/callback`). Session is httpOnly cookie `mtf_session`. Craig and Jessica keep private `WATCH_USERS` username+password (`X-User-Id` + `X-API-Secret`). Consumer records live in Netlify Blobs store `mtf-users`, not Gist. Env (values never in git): `MAGIC_LINK_SECRET`, `RESEND_API_KEY`, `MAGIC_LINK_FROM`.

**Hybrid billing (Slice 3):** Single Watch is a one-time Stripe Checkout (`mode=payment`) for one billable watch, **$4.99**, until the last date passes. Planner is **$14.99/month** for up to **4** active alerts. Same Stripe Customer. Checkout is created only after login. Stripe webhooks own consumer active/inactive. Craig and Jessica (`craig`, `Jessica`) stay unrestricted: no Stripe, no cap, no Checkout. Consumers without Planner get HTTP 402 `checkout_required` + `checkout_url` (not a Gist write). Live Planner under cap writes watches in-app (201). Show $4.99 and $14.99/month in the SPA at landing and pay/upgrade moments.

**Global consumer watch budget (Slice 4):** `CONSUMER_ACTIVE_WATCH_BUDGET` (default **40**) is a hard cap on **active consumer date rows** in `watches.json` — the same rows the VPS poller scans. Only a strict positive integer is honored; malformed values (`20x`, `1.5`, `0`) fall back to 40. Craig and Jessica are unrestricted and do not consume the budget. Enforce at `POST /watches` (before Checkout or Gist write) and again at Checkout fulfillment. At/over cap → HTTP **503** `{ code: 'watch_budget' }` (no silent drop). Unpaid watches still never hit Gist. Do not change the 10-minute poll interval.

**Competitive notes (dining selection):**

- Steal from MouseDining: typeahead, park grouping, restaurant facts. Do not steal their public availability calendar as the home screen, or one-meal-one-date alert slots.
- Steal from MouseWatcher: create-alert as the job; pay before a consumer watch is live (Slice 3); booking link in SMS (already shipped). Do not steal their 3-date cap or “costs more the further out you book.”
- Magic Table Finder already wins on new-opening-only alerts, multi-date/multi-meal watches, and owner-scoped SMS.

---

## Architecture

```
VPS systemd timer (every 10 min)
    └── disney_bot.py --once      → Xvfb + persistent Playwright profile → Disney API → owner-specific WhatsApp/SMS alert

GitHub Gist (state store — ID: 7e8d8f873715971f8989a25a2f22c089)
    ├── watches.json       — owner-scoped watch records
    ├── config.yaml        — legacy grouped watch list, read only for migration/fallback
    ├── bot_state.json     — last poll timestamp + health state
    ├── open_slots.json    — previous poll's open-slot baseline for new-opening detection
    ├── seen_slots.json    — audit/history of successfully notified slots
    └── calendar_{fid}.json  — pre-cached available dates per restaurant

Netlify (magictablefinder.com, site ID: b1f7efc5-da94-4159-ade0-568de33ed24f)
    ├── public/index.html — SPA frontend (profile login + create-watch form + date picker)
    └── netlify/functions/api.js — Node.js Lambda, reads/writes Gist
```

---

## The Akamai Problem (Critical)

Disney's CDN (Akamai) blocks Python HTTP requests with HTTP 428. The `_abck` cookie is tied to a real browser session and cannot be replayed reliably.

**The fix:** All Disney API calls run inside a persistent Playwright Chrome profile on the VPS:
1. Playwright navigates Chrome to `/dine-res/restaurant/{slug}`.
2. Waits for Akamai/browser session warm-up.
3. Reads the Disney auth token from the Playwright profile cookies.
4. Runs `fetch()` from within the page context.

**Key constraint:** only the first API call after a fresh page load is reliable. Navigate to each restaurant page before fetching its data.

**Session requirement:** the VPS Playwright profile must be logged into Disney. If Disney expires or challenges the session, run `DISNEY_HEADLESS=false xvfb-run -a python3 seed_disney_session.py` on the VPS and log in again.

On the headless VPS, Playwright must run under `xvfb-run -a` unless it is explicitly headless. The systemd service handles this. Manual live polls should use:

```bash
xvfb-run -a python3 disney_bot.py --once
```

---

## Scheduled Activities (Enchanting Extras, e.g. Harmony Barber Shop)

Some watchable experiences are NOT in the dine-res dining API. They book
through Disney's Scheduled Activity system on the same origin (verified live
2026-07-05):

- Watch records carry `booking_type`: `"dining"` (default) or
  `"scheduled_activity"`. `check_slots_for_restaurant` dispatches on it.
- Details: `GET /sa-api/api/v1/experience/details/{slug}/` → bookable window
  (rolling ~60 days), `schedules` (operating days), `availableDaysDateRange`.
- Availability: `GET /sa-api/api/v1/experience/offers/{productId}?entityType=activity-product&startDate=…&endDate=…&adult=N`
  — date windows must span ≤ 9 days; available offers have `startTime` +
  `timePeriod` ("Morning"/"Afternoon"), which maps to `Slot.meal_period` so
  dedupe/alert semantics are unchanged. sa-api auths via browser cookies
  (no bearer token); warm up on `/enchanting-extras-collection/{slug}/`.
- These watches always use `meal_periods: ["ALL"]`; the time window is the
  filter. Harmony Barber Shop: product id 15437454, max 2 guests per order
  (`max_party_size` in restaurants.json, enforced by api.js and the UI).
- `index_restaurants.py` re-merges `SPECIALTY_EXPERIENCES` on every run —
  add new Enchanting Extras experiences there, not just restaurants.json.
- Smoke test: `xvfb-run -a python3 scripts/smoke_test_sa_api.py` on the VPS.

---

## Key Files

| File | Purpose |
|------|---------|
| `disney_bot.py` | Main polling loop; reads owner-scoped watches, deduplicates, sends SMS |
| `watch_store.py` | Owner/profile-aware watch storage; migrates legacy `config.yaml` |
| `monitor.py` | Disney API integration; `check_slots_via_playwright()` uses hosted Chrome |
| `auth.py` | Legacy Disney token helpers; useful for token parsing reference, but production polling uses Playwright/browser cookies in `monitor.py` |
| `update_calendar_cache.py` | Caches available date ranges per restaurant to Gist |
| `storage.py` | Gist/local file abstraction |
| `notify.py` | Twilio SMS formatting and owner-specific delivery |
| `netlify/functions/api.js` | REST endpoints for status, profiles, restaurants, calendars, watches |
| `public/index.html` | Frontend SPA |
| `seed_disney_session.py` | Opens the VPS Playwright profile for manual Disney login |
| `scripts/smoke_test_api.py` | Safe production API smoke test; creates/deletes fake future watches |
| `scripts/sync_disney_login_to_vps.py` | Safely pushes Disney credentials to the VPS `.env` and seeds the session |
| `public/privacy.html` | Public privacy policy required for Twilio A2P compliance |
| `public/terms.html` | Public terms of service required for Twilio A2P compliance |
| `tests/test_alert_semantics.py` | Unit tests for new-opening alert behavior |
| `deploy/` | systemd service/timer templates for the VPS worker |

---

## Running Manually

```bash
cd ~/CursorProjects/disney-dining-bot

# Local syntax/tests:
node --check netlify/functions/api.js
PYTHONPYCACHEPREFIX=.pycache python3 -m unittest tests.test_alert_semantics
PYTHONPYCACHEPREFIX=.pycache python3 -m py_compile disney_bot.py monitor.py notify.py watch_store.py update_calendar_cache.py seed_disney_session.py

# Safe live API smoke test. Uses a fake future watch and cleans it up:
python3 scripts/smoke_test_api.py --user-id craig
python3 scripts/smoke_test_api.py --user-id Jessica
```

On the VPS:

```bash
cd /opt/disney-dining-bot
. .venv/bin/activate

# Seed or repair Disney login session:
DISNEY_HEADLESS=false xvfb-run -a python3 seed_disney_session.py

# Test a full worker poll:
xvfb-run -a python3 disney_bot.py --once
```

---

## Credentials

All credentials live in `.env` (never committed). Key vars:

```
WATCH_USERS — JSON mapping user IDs to {name,password,phone}
API_SECRET — legacy shared login fallback
GITHUB_TOKEN — PAT for Gist read/write
GITHUB_GIST_ID — 7e8d8f873715971f8989a25a2f22c089
TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN — Twilio API credentials
TWILIO_MESSAGING_SERVICE_SID — A2P 10DLC-registered Messaging Service (primary path for `+1...` SMS recipients). Twilio picks the registered sender from the pool; do not also set TWILIO_FROM for SMS.
TWILIO_FROM — fallback sender. For WhatsApp recipients set to `whatsapp:+1...` (sandbox). For SMS, only used when TWILIO_MESSAGING_SERVICE_SID is unset (dev only — unregistered traffic is rejected by US carriers).
TWILIO_STATUS_CALLBACK_URL — public DisScan webhook URL used by Twilio to reconcile queued messages to terminal delivered or failed status. Omit safely in local development.
SIGNAL_BOT_NUMBER — phone number the bot's signal-cli account sends FROM (alternative channel; recipients prefixed `signal:`)
SIGNAL_CLI_PATH — optional override for signal-cli binary (default /usr/local/bin/signal-cli)
DISNEY_BROWSER_PROFILE_DIR — persistent Playwright profile path
DISNEY_HEADLESS — MUST be "false" on the VPS. Akamai detects truly-headless Chromium on disneyworld.disney.go.com and aborts with ERR_HTTP2_PROTOCOL_ERROR. Run headed under xvfb-run instead.
DISNEY_LOGIN_EMAIL — dedicated Disney bot account email (VPS only)
DISNEY_LOGIN_PASSWORD — dedicated Disney bot account password (VPS only)
DISNEY_FRIENDS_DATA_PATH — absolute path to the root-only Friends & Family runtime capture outside the Git checkout
DISNEY_RECOVERY_LOG_PATH — optional override for the full Login Agent log (default /var/log/disney-dining-bot/last-recovery.log)
DISNEY_ALERT_ADMIN — optional WATCH_USERS user_id to receive operational alerts; defaults to default_owner_id (craig)
PLANNER_WATCH_CAP — optional Planner per-account billable-watch cap (default 4). Netlify only.
CONSUMER_ACTIVE_WATCH_BUDGET — optional global cap on active consumer watches.json date rows the poller scans (default 40). Netlify only. Craig/Jessica unrestricted.
```

Each `WATCH_USERS[*].phone` value is a channel-prefixed recipient handled by `notify.py`'s dispatcher:

- `+15551234567` → Twilio SMS via the A2P 10DLC Messaging Service (current production path). Requires `TWILIO_MESSAGING_SERVICE_SID` to be set so US carriers accept the traffic.
- `signal:+15551234567` → Signal via local signal-cli (alternative channel; still supported)
- `signal:<account-uuid>` → Signal by account UUID, used when the recipient has Signal phone-number-discoverability disabled
- `whatsapp:+15551234567` → Twilio WhatsApp sandbox (dev / fallback only — sandbox opt-in expires every 72h, do not rely on this)

Operational alerts (session expired, recovery failed) route only to the admin via `_configured_owner_phones()`, not to all owners. Reservation alerts remain owner-scoped per-watch.

Never print full phone numbers, tokens, passwords, or `.env` contents.

Security note: an old slash-command example previously contained the live legacy `API_SECRET`. Treat that secret as exposed in git history and rotate it in Netlify, the VPS `.env`, and any local `.env` before relying on it for real access control. Prefer per-user `WATCH_USERS[*].password` over the legacy shared `API_SECRET`.

## Alert Semantics

This is the most important behavior to preserve.

- A watch belongs to exactly one owner (`owner_id`), and alerts route only to that owner's configured phone.
- `monitor.py` returns every currently open matching Disney slot.
- `disney_bot.py` compares current open slots to `open_slots.json`, the previous poll's open-slot snapshot.
- Alert only when a stable visible slot is open now but was absent in the previous poll.
- Stable slot identity is: `watch_id`, `facility_id`, `date`, `time`, `meal_period`, `party_size`.
- Do **not** include `offer_id` in the dedupe key. Disney may rotate `offerId` for the same visible time. Including it caused false duplicate alerts.
- `offer_id` is still included in the booking URL when available because it may help deep-link to the specific offer.
- Continuously open slots must stay quiet.
- A slot that disappears and later reappears should alert again.
- If a restaurant poll fails, preserve that restaurant's previous baseline so the next successful poll does not spam old openings.
- If notification delivery fails for a recipient, keep those slots out of the baseline so they retry, and record the error in `bot_state.json`.
- `seen_slots.json` is an audit/history of successfully sent notifications, not the primary dedupe mechanism.

Alert copy should list only newly opened slots. Do not group a new slot back into a full list of all open times. All alerts must append `Reply STOP to opt out. Reply HELP for help.` to comply with Twilio A2P 10DLC requirements.

## Frontend UX Principles

- Create Watch is the primary workflow.
- Date selection is calendar-first on mobile: prominent `Choose Dates`, selected chips, explicit `Done`, manual date typing behind `Enter dates manually`.
- Manual date input is a fallback/power-user path. Do not make users type `YYYY-MM-DD` as the default mobile flow.
- Keep restaurant, party size, dates, meal periods, and time window visible before submit.
- Preserve owner/profile clarity. Craig and Jessica should never wonder whose phone gets the alert.
- Do not expose phone numbers, Gist IDs, tokens, passwords, or internal Disney IDs in the UI.
- **Twilio A2P 10DLC Compliance:** The UI must block watch creation until the user explicitly checks an unchecked SMS consent checkbox. The public `/privacy.html` and `/terms.html` pages must remain available and explicitly state that mobile opt-in data and SMS consent are not shared or sold.
- Guests build the watch before identity (restaurant → dates → phone → email → consent → Continue).
- The draft lives in `localStorage.mtfWatchDraft` and resumes after the magic link.
- Login still precedes Stripe Checkout (D3).
- The catalog for guests is the static `/restaurants.json`.

---

## Deployment

```bash
# Deploy frontend + Netlify function. main triggers Netlify:
git add <files>
git commit -m "..."
git push origin cursor/self-hosted-mousewatcher
git push origin HEAD:main

# VPS worker sync after worker-code changes:
ssh -i "$HOME/.ssh/disney_dining_vps" root@${DISNEY_VPS_HOST} \
  'cd /opt/disney-dining-bot && git pull --ff-only origin cursor/self-hosted-mousewatcher && systemctl is-active disney-dining-bot.timer'
```

For alert-semantic changes, pause the timer before risky validation, seed/baseline carefully, then re-enable:

```bash
systemctl stop disney-dining-bot.timer
# run tests, sync code, and baseline current open slots if needed
systemctl start disney-dining-bot.timer
```

Never force-push `main`. Never reset or delete Gist state unless explicitly requested and you understand the alert consequences.

---

## Health Checks

1. `journalctl -u disney-dining-bot.service -n 100` — look for successful Playwright polls and no notification errors.
2. `curl -H "X-User-Id: craig" -H "X-API-Secret: <password>" https://magictablefinder.com/_api/status` — check `session_status`, `last_poll_at`, and `last_errors`.
3. `python3 scripts/smoke_test_api.py --user-id craig` and `--user-id Jessica` — verifies profiles, owner scoping, create/delete cleanup.
4. Check Gist directly only when necessary: `https://gist.github.com/7e8d8f873715971f8989a25a2f22c089`

---

## Known Issues / Gotchas

For end-to-end diagnostic patterns, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md). Highlights:

- **Disney's refresh endpoint is dead.** `auth.registerdisney.go.com/v4/.../refreshAuth` currently returns Akamai 403 ("distribution supports only cachable requests"). The bot logs the response body verbatim but no longer treats refresh failure as fatal — `monitor.py:_bearer_from_token_state` and `auth.py:get_valid_token` fall back to the unrefreshed access token whenever it still has > 60 s of life. Disney's web layer renews the cookie on its own as soon as Playwright navigates to a Disney page, which is what keeps the bot alive.
- **`DISNEY_HEADLESS=false` is mandatory.** Truly-headless Chromium (`--headless=new`) is detected by Akamai and aborted with `ERR_HTTP2_PROTOCOL_ERROR` before any page renders. Run headed under `xvfb-run -a` (the systemd service does this). The Login Agent recovery subprocess in `disney_bot.py` explicitly forces this — do not "fix" it back to true.
- **Login Agent cooldown vs. timer.** Cooldown is 9 minutes, timer fires every 10 minutes, so every poll *can* attempt recovery if needed. Don't raise the cooldown above the timer interval — that reintroduces a guaranteed silent-failure window.
- **Recovery log.** The most recent automated recovery attempt's full stdout/stderr lives at `/var/log/disney-dining-bot/last-recovery.log` (configurable via `DISNEY_RECOVERY_LOG_PATH`).
- **SMS "manual re-seed" alert has a grace window.** It is *not* triggered by a single failed recovery. It fires only after `AUTH_ALERT_THRESHOLD` (3) consecutive auth-failed polls (~30 min), so a transient Akamai/refresh-403 blip that self-heals next poll stays silent. The counter is `bot_state.json:consecutive_auth_failures` (increments on each auth-failed poll, resets to 0 on any clean poll). A re-alert cooldown of 6h still applies via `last_session_manual_alert_at`; a clean poll clears that timestamp so a genuinely new outage can page after its own grace window. To force a fresh test alert, set `consecutive_auth_failures` ≥ 3 and clear `last_session_manual_alert_at`. Background recovery (9-min Login Agent cooldown) is unchanged and still runs every cycle during the grace window.
- **Stale `SingletonLock` in `.browser-profile/`** can crash a manually-launched seeder with an immediate Node `EPIPE` traceback. Remove `.browser-profile/SingletonLock`, `SingletonCookie`, `SingletonSocket` and retry.
- **Disney session expired:** push credentials from macOS via `pbpaste | python3 scripts/sync_disney_login_to_vps.py --password-stdin --run-seed-and-poll`. If manual MFA/CAPTCHA completion is required, omit `--run-seed-and-poll` and SSH in with a TTY to run `DISNEY_HEADLESS=false xvfb-run -a python3 seed_disney_session.py` so you can interact with the browser.
- **Dedicated Disney bot login:** `DISNEY_LOGIN_EMAIL` and `DISNEY_LOGIN_PASSWORD` live only in the VPS `.env`. `seed_disney_session.py` attempts automated login and verifies the Disney auth cookie via `_fill_first_any_frame`.
- **428 errors:** re-seed the VPS browser session and verify `DISNEY_BROWSER_PROFILE_DIR` persists across worker runs.
- **Missing X server / Playwright headed error:** run manual worker commands under `xvfb-run -a` on the VPS.
- **Worker not running:** `systemctl status disney-dining-bot.timer`.
- **Gist rate limit/conflict:** rare. Wait a few minutes; the 10-minute polling interval should avoid this normally.
- **Unexpected alert spam:** inspect `open_slots.json` and `bot_state.json`; do not clear state casually. Verify `_slot_key()` still excludes ephemeral `offer_id`.
- **Twilio spend spike:** check Twilio usage categories. A2P/phone-number setup fees can dwarf actual WhatsApp/SMS traffic.

## Agent Operating Model

When multiple agents are available:

- Owner: Cursor. Implements, reviews, commits, deploys, production safety.
- Builder: Grok 4.6 in Cursor.
- Do not launch Goose or Claude.
- QA/ops checks: smoke tests, VPS timer state, bot health, Twilio usage, no leftover fake watches.

For meaningful changes:

1. Write a short spec and acceptance criteria.
2. Implement narrowly.
3. Run local syntax/unit checks.
4. Get senior review for subtle/risky work.
5. Smoke-test production.
6. Commit, deploy, verify health.
