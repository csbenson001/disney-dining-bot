# Troubleshooting — Disney Dining Bot

A runbook for diagnosing the bot from the VPS without reading any source. Read top to bottom; each section is self-contained.

VPS: `root@${DISNEY_VPS_HOST}`, SSH key `~/.ssh/disney_dining_vps`, app at `/opt/disney-dining-bot`.

## At-a-glance: alert channels

| Alert source | Trigger | Goes to | Channel |
|---|---|---|---|
| Bot's Signal alert (in-process) | Disney session lost; Login Agent failed | Craig only | Signal (`${SIGNAL_BOT_NUMBER}` "Magic Table Finder" → Craig) |
| Reservation match | New opening on a watch | Watch's owner only | Signal |
| UptimeRobot | `/_api/health` returns non-200 for >5 min, or no response for >30 min | `${ALERT_CONTACT_EMAIL}` | Email |

If you haven't gotten an alert and feel anxious: hit https://magictablefinder.com/_api/health from any browser. `"ok":true` means the bot is healthy — Disney just doesn't have anything matching your watches yet.

---

## 1. Quick health check

```bash
ssh -i ~/.ssh/disney_dining_vps root@${DISNEY_VPS_HOST}
cd /opt/disney-dining-bot
systemctl is-active disney-dining-bot.timer    # should be "active"
journalctl -u disney-dining-bot.service -n 50 --no-pager
```

A healthy poll looks like:

```
=== poll start ===
Polling 21 watch(es) across 2 restaurant request(s) …
[monitor] Space 220 Lounge: no availability returned by Disney.
[bot] Space 220 Lounge: no availability.
[bot] No newly opened slots to alert on.
=== poll done ===
```

"No availability" is **normal** — it just means Disney has nothing matching your watches right now. It is *not* an error.

If polls succeed but you expect alerts, that's a watch/criteria question, not a bot health question. Skip the rest of this doc and check `magictablefinder.com`.

---

## 2. Failure modes (what to look for in journalctl)

The bot has three known classes of failure. Grep the logs and match the pattern.

### 2.1 Disney session genuinely expired (most common)

```
[bot] Check failed for Space 220 Lounge: Disney auth required:
  token cookie present but not refreshable; ...
```

Plus, near it, one of:
- `[auth] Disney cookie token expires in -NNNNs — refreshing.` (negative seconds = past expiry)
- `[auth] refresh non-200: HTTP 403 from https://auth.registerdisney.go.com/...`

**Diagnosis:** the persistent Chrome profile's Disney cookie expired or was invalidated. Disney's refresh endpoint (`auth.registerdisney.go.com/.../refreshAuth`) currently returns Akamai 403 for everyone — that endpoint is effectively dead, so we cannot refresh programmatically. The session must be re-established via a real browser login.

**Fix:** re-seed (see §3).

### 2.2 Login Agent failing to recover automatically

```
[bot] Disney auth error detected — activating Login Agent...
[recovery] Attempting automatic Disney session recovery...
[recovery] Recovery failed (exit 1). Full log: /var/log/disney-dining-bot/last-recovery.log...
```

**First read the full recovery log:**

```bash
cat /var/log/disney-dining-bot/last-recovery.log
```

Common patterns inside:

| Recovery-log pattern | What it means | Fix |
|---|---|---|
| `net::ERR_HTTP2_PROTOCOL_ERROR` | Akamai detected headless Chromium and aborted | Confirm `DISNEY_HEADLESS=false` in `.env` and that recovery subprocess is not overriding it (see §6) |
| `Could not find a visible email field` | Disney login page selectors changed, or page didn't load | Manually re-seed in a TTY (§3); update selectors in `seed_disney_session.py` if persistent |
| `additional verification step required` | MFA / CAPTCHA / passkey was prompted | Manually re-seed in a TTY (§3) and complete the challenge |
| `WARNING: Disney auth cookie was not found` | Login appeared to succeed but the auth cookie wasn't set | Manually re-seed in a TTY (§3); Disney may have invalidated the dedicated bot account |

After a Login Agent failure, owners should receive an SMS within ~10 min telling them manual re-seed is required. If you don't see that SMS, see §5.

### 2.3 Login Agent on cooldown

```
[bot] Login Agent on cooldown (last attempt 2026-XX-XXTXX:XX:XXZ).
[bot] Skipping Login Agent due to cooldown.
```

**Not an error by itself** — the bot retries at most every 9 minutes. Just wait one cycle. If you need to clear the cooldown manually:

```bash
# Edit bot_state.json in the Gist (or local file). Remove or zero out
# "last_login_agent_attempt_at". The next poll will retry recovery.
```

In practice, manually re-seeding (§3) is faster and resets the same condition.

### 2.4 Other miscellaneous failures

* `HTTP 428` — Disney's Akamai is challenging us. Same fix as §2.1: re-seed.
* `RuntimeError: Playwright is not installed` — `pip install -r requirements.txt && python3 -m playwright install chromium` inside the venv.
* `EPIPE` / Node driver crash when running the seeder by hand — usually a stale `SingletonLock` in the Chrome profile (see §4).

---

## 3. Manually re-seed the Disney session

This is the universal fix when the bot says "Disney auth required" or "Re-seed the VPS browser profile".

```bash
ssh -t -i ~/.ssh/disney_dining_vps root@${DISNEY_VPS_HOST} \
  'cd /opt/disney-dining-bot && . .venv/bin/activate && \
   DISNEY_HEADLESS=false xvfb-run -a python3 seed_disney_session.py'
```

The `-t` allocates a TTY so you can press Enter when login completes. The script:

1. Opens a virtual-display Chrome under xvfb.
2. Navigates to a known-working Disney restaurant page, then clicks Sign In.
3. Tries automated login if `DISNEY_LOGIN_EMAIL` / `DISNEY_LOGIN_PASSWORD` are set in `.env`.
4. If automation fails (MFA, captcha, selector drift), it waits for you to press Enter — meaning you'd need to interact with the headed browser, which over xvfb you can't see directly.

**If the seeder needs visible interaction** (MFA, captcha, manual login), you have two options:

* **Push credentials and let it auto-login** (works if your dedicated bot account has MFA disabled / device trusted):
  ```bash
  # On macOS, copy your DISNEY password to clipboard, then:
  pbpaste | python3 scripts/sync_disney_login_to_vps.py --password-stdin --run-seed-and-poll
  ```
* **Forward X over SSH so you can see the browser:**
  ```bash
  ssh -X -t -i ~/.ssh/disney_dining_vps root@${DISNEY_VPS_HOST} \
    'cd /opt/disney-dining-bot && . .venv/bin/activate && \
     DISNEY_HEADLESS=false python3 seed_disney_session.py'
  # Note: no xvfb-run, and you need XQuartz on macOS.
  ```

**Verify success:**

```bash
ssh -i ~/.ssh/disney_dining_vps root@${DISNEY_VPS_HOST} \
  'systemctl start disney-dining-bot.service; sleep 60; \
   journalctl -u disney-dining-bot.service -n 30 --no-pager --since "2 minutes ago"'
```

You want to see `=== poll done ===` with no `Check failed` lines.

---

## 4. Stale `SingletonLock` (manual seeder crashes immediately with EPIPE)

If running `seed_disney_session.py` from an SSH session crashes within seconds with a Node.js `EPIPE` traceback (no `[seed]` log lines), Chrome cannot acquire the profile because of a leftover lock from a previously-killed Chromium.

```bash
ssh -i ~/.ssh/disney_dining_vps root@${DISNEY_VPS_HOST} \
  'rm -f /opt/disney-dining-bot/.browser-profile/SingletonLock \
         /opt/disney-dining-bot/.browser-profile/SingletonCookie \
         /opt/disney-dining-bot/.browser-profile/SingletonSocket'
```

Also make sure no rogue Chromium is still running:

```bash
ssh -i ~/.ssh/disney_dining_vps root@${DISNEY_VPS_HOST} 'pgrep -af chromium; pgrep -af xvfb-run'
# kill any leftover processes if found
```

Then retry §3.

---

## 5. Signal alerts when the bot needs attention

When the Login Agent fails, the worker sends a Signal message **to Craig only** (the admin, default owner). Jessica gets her own reservation alerts via Signal but does not receive operational alerts. Rate-limited to one alert per 6 hours.

The body looks like:

```
Disney Dining Bot: Disney login session failed after automatic recovery.
Manual re-seed required. On a machine with your SSH key:
ssh -i ~/.ssh/disney_dining_vps -t root@${DISNEY_VPS_HOST} 'cd /opt/disney-dining-bot && . .venv/bin/activate && DISNEY_HEADLESS=false xvfb-run -a python3 seed_disney_session.py'
```

### How alerts are routed

`notify.py` is a channel dispatcher. Each `WATCH_USERS[*].phone` value carries a prefix:

- `signal:+15551234567` → POSTs to local `signal-cli` on the VPS
- `signal:<account-uuid>` → same, used when the recipient has Signal phone-number-discoverability disabled (Craig's case — his Signal account UUID is stored, not his phone number)
- `whatsapp:+15551234567` → Twilio WhatsApp (legacy / fallback only)
- `+15551234567` → Twilio SMS (legacy / fallback only)

The bot's Signal sender account is configured privately in `.env` as `SIGNAL_BOT_NUMBER`. Operational alerts route to whatever `_configured_owner_phones()` returns, which is *only the admin's phone* — admin is `default_owner_id()` (i.e. `craig`) unless overridden via `DISNEY_ALERT_ADMIN`.

### If you didn't get a Signal alert but the bot is clearly broken

* Check `journalctl` for `[notify] signal-cli exit ...` or `[notify] signal-cli timed out` — signal-cli on the VPS hit an issue. Check `signal-cli -a ${SIGNAL_BOT_NUMBER} listAccounts` works.
* Check `journalctl` for `[bot] Recovery failed but no owner phones are configured for alerting.` — `WATCH_USERS` in `.env` is missing the admin's phone.
* Check `bot_state.json:last_session_manual_alert_at` — if it's within the last 6 hours, you're inside the rate-limit window. The bot still needs a re-seed; you just won't get another Signal alert yet. Clear that field in the Gist if you need a fresh test alert.
* If signal-cli fails with `Unregistered user`: the recipient's Signal account hides their phone number. Have them message the bot's Signal number first, then run `signal-cli -a ${SIGNAL_BOT_NUMBER} receive` once on the VPS to learn their UUID, then store `signal:<uuid>` in `WATCH_USERS` instead of `signal:+phone`.

## 5a. UptimeRobot — the external watchdog

The in-bot Signal alert can only fire when the bot is alive enough to detect a failure. If the VPS itself is down or the systemd timer dies, that alert never sends. UptimeRobot covers that case.

- **Monitor:** HTTP GET `https://magictablefinder.com/_api/health` every 5 minutes.
- **Healthy:** returns `200 {"ok":true,"last_successful_poll_at":"...","age_minutes":N}` when the bot has polled within the last 30 minutes.
- **Unhealthy:** returns `503 {"ok":false,"reason":"..."}` when stale or no poll on record.
- **Alert contact:** email to `${ALERT_CONTACT_EMAIL}`. Make sure the email app on Craig's phone has push notifications enabled.

If you want to tune the staleness threshold, set `HEALTH_STALE_MINUTES` in Netlify env vars (default 30).

---

## 6. Operational invariants you must preserve

These are subtle and have caused outages before. Keep them in mind whenever editing `.env` or the systemd unit.

### 6.1 `DISNEY_HEADLESS=false` in `.env`

Akamai detects "true" headless Chromium (`--headless=new`) on `disneyworld.disney.go.com` and aborts the connection with `ERR_HTTP2_PROTOCOL_ERROR` before any page renders. Even though the systemd service has no display, we run Chromium **headed** under `xvfb-run -a` (a virtual display). This is what `DISNEY_HEADLESS=false` does.

If you set `DISNEY_HEADLESS=true`, every poll and every recovery attempt will fail at navigation. The recovery subprocess in `disney_bot.py` explicitly forces `DISNEY_HEADLESS=false` to mirror the worker; do not "fix" that override back to true.

### 6.2 Refresh endpoint is dead — do not rely on it

`https://auth.registerdisney.go.com/v4/client/TPR-WDW-LBJS.WEB-PROD/guest/refreshAuth` currently returns Akamai 403 ("This distribution is not configured to allow the HTTP request method that was used for this request"). The bot logs this body verbatim now, but does **not** treat it as fatal — `monitor.py:_bearer_from_token_state` and `auth.py:get_valid_token` fall back to the unrefreshed access token whenever it still has > 60 s of life.

Disney's web layer renews the cookie on its own as soon as Playwright navigates to a Disney page, so we don't actually need the refresh endpoint while a session is live. We only need a re-seed when the cookie genuinely expires.

If you see a refresh succeed in the logs (`[auth] Disney token refreshed and saved.`), the endpoint has come back. Great. The fallback still protects you when it goes away again.

### 6.3 Persistent profile dir

`DISNEY_BROWSER_PROFILE_DIR=/opt/disney-dining-bot/.browser-profile` must persist across worker runs and reboots. The Disney auth cookie lives there. If the directory is wiped, you must re-seed (§3).

The systemd service does *not* touch this directory on startup. Be careful with `chown` / cleanup commands inside the app dir.

### 6.4 Login Agent cooldown vs. poll interval

* systemd timer: every 10 min (`OnUnitActiveSec=10min`)
* Login Agent cooldown: 9 min

These are tuned so every poll *can* attempt recovery if needed. If you raise the cooldown above the timer interval, you reintroduce a guaranteed silent-failure window — don't do that without thinking.

### 6.5 SMS alert rate-limit

After a Login Agent failure SMS, the worker waits 6 hours before sending another. This lives in `bot_state.json` as `last_session_manual_alert_at`. If you actively want a fresh test alert, clear that field in the Gist or wait for the window.

---

## 7. Diagnostic command cheat sheet

```bash
# Quickest "is the bot alive?" check, runs from any browser:
#   https://magictablefinder.com/_api/health
# Returns {"ok":true,...} or {"ok":false,...}

# Tail live logs
journalctl -u disney-dining-bot.service -f

# Last 200 lines, all errors and recovery events
journalctl -u disney-dining-bot.service -n 200 --no-pager | \
  grep -iE '(Check failed|recovery|Login Agent|auth|cookie|HTTP2|seed|SUCCESS)'

# Full output of the most recent automated recovery attempt
cat /var/log/disney-dining-bot/last-recovery.log

# Signal: confirm the bot's account is registered and reachable
signal-cli -a ${SIGNAL_BOT_NUMBER} listAccounts
signal-cli -a ${SIGNAL_BOT_NUMBER} send -m "manual test" <recipient>

# Manually re-seed Disney session (the canonical fix for "auth required" errors)
cd /opt/disney-dining-bot && . .venv/bin/activate && \
  DISNEY_HEADLESS=false xvfb-run -a python3 seed_disney_session.py

# Force one immediate poll (don't wait for the 10-min timer)
systemctl start disney-dining-bot.service
sleep 60
journalctl -u disney-dining-bot.service -n 50 --no-pager --since "2 minutes ago"

# Pause the bot before risky changes
systemctl stop disney-dining-bot.timer
# ... do work ...
systemctl start disney-dining-bot.timer

# Check next scheduled run
systemctl list-timers disney-dining-bot.timer --no-pager

# Verify .env (without leaking secrets)
grep -E '^(DISNEY_|TWILIO_FROM|GITHUB_GIST_ID|WATCH_USERS)=' /opt/disney-dining-bot/.env | \
  sed -E 's/(PASSWORD|TOKEN|AUTH_TOKEN|EMAIL)=.*/\1=***/'
```

---

## 8. Escalation path

If the runbook above doesn't restore the bot in 30 minutes:

1. **Confirm Disney's site itself is up** in a normal browser. Major Disney outages do happen — there's nothing to fix on our side.
2. **Check whether Disney's API contract changed.** Symptom: polls navigate fine but `[monitor] {restaurant}: HTTP NNN from availability API`. Inspect the URL the worker calls (`/dine-res/api/availability/...`) in a real browser's network tab; if the path or required headers changed, `monitor.py` needs updating.
3. **Check the dedicated Disney bot account itself.** Log in manually from any browser. If Disney has flagged the account or forced a password reset, no amount of re-seeding will help until you fix that account.
4. **Pause the timer** so the bot stops sending duplicate failure alerts while you investigate:
   ```bash
   systemctl stop disney-dining-bot.timer
   ```
   Restart it once you've fixed whatever broke.
