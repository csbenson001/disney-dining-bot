# VPS Deployment

This project now runs like a self-hosted MouseWatcher:

- Netlify hosts `magictablefinder.com`.
- Gist stores watches, status, calendar caches, `open_slots.json`, and alert history.
- A low-cost VPS runs the Playwright polling worker every 10 minutes.
- Twilio texts/WhatsApps the user who created each watch.

## 1. Install Runtime

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip
git clone <repo-url> /opt/disney-dining-bot
cd /opt/disney-dining-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install --with-deps chromium
```

## 2. Configure `.env`

Copy `.env.template` to `.env` and set:

```bash
# Each user's phone field uses a channel prefix. See notify.py and PRODUCT.md.
# signal:+1...         -> Signal via local signal-cli (current production)
# signal:<uuid>        -> Signal by account UUID (use when the recipient has
#                         "Phone Number Discoverability" off in Signal Privacy)
# whatsapp:+1... / +1... -> Twilio (legacy / fallback only)
WATCH_USERS='{"craig":{"name":"Craig","password":"...","phone":"signal:<uuid-or-+1...>"},"Jessica":{"name":"Jessica","password":"...","phone":"signal:+1..."}}'
GITHUB_GIST_ID=7e8d8f873715971f8989a25a2f22c089
GITHUB_TOKEN=...

# Signal — primary alert channel
SIGNAL_BOT_NUMBER=+1...           # Google Voice number registered with Signal
                                  # via signal-cli on this VPS (see § Signal Setup)

# Twilio — legacy / fallback only; not the primary path anymore
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM=+1...
TWILIO_STATUS_CALLBACK_URL=https://disscan.com/api/webhooks/twilio/status

DISNEY_BROWSER_PROFILE_DIR=/opt/disney-dining-bot/.browser-profile
DISNEY_HEADLESS=false
DISNEY_LOGIN_EMAIL=...           # dedicated bot Disney account
DISNEY_LOGIN_PASSWORD=...        # dedicated bot Disney account
```

> **Important:** `DISNEY_HEADLESS` must be `false`. Disney's Akamai aborts truly-headless Chromium (`--headless=new`) with `ERR_HTTP2_PROTOCOL_ERROR`. The systemd service runs the worker under `xvfb-run -a`, giving Chromium a virtual display, so "headed" Chromium runs fine without a real GUI. The recovery subprocess explicitly mirrors this. Do not flip this back to `true`.

Netlify needs the same `WATCH_USERS`, `GITHUB_GIST_ID`, `GITHUB_TOKEN`, and legacy `API_SECRET` fallback if you keep it.
If WATCH_USERS is not set in Netlify, it will use FALLBACK_USERS (default: craig and Jessica profiles without phones) for public profile exposure.

## 3. Seed Disney Session

```bash
cd /opt/disney-dining-bot
. .venv/bin/activate
DISNEY_HEADLESS=false xvfb-run -a python3 seed_disney_session.py
```

Log into Disney in the browser that opens, then press Enter in the terminal. The worker reuses that profile.

## 3a. Signal alert channel (signal-cli)

The bot sends alerts via [`signal-cli`](https://github.com/AsamK/signal-cli) running on this VPS, registered to a dedicated phone number (a free Google Voice number works).

```bash
# Install (one-time). 1GB swap is recommended on a 1GB VPS to give the JVM
# headroom alongside Chromium:
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

sudo apt-get install -y openjdk-21-jre-headless
LATEST=$(curl -sL https://api.github.com/repos/AsamK/signal-cli/releases/latest | grep tag_name | sed -E 's/.*"v?([0-9.]+)".*/\1/')
sudo mkdir -p /opt/signal-cli && cd /opt/signal-cli
sudo curl -sL "https://github.com/AsamK/signal-cli/releases/download/v${LATEST}/signal-cli-${LATEST}-Linux-native.tar.gz" | sudo tar xz --strip-components=0
sudo ln -sf /opt/signal-cli/signal-cli /usr/local/bin/signal-cli

# Register the bot's number. Signal will demand a CAPTCHA — solve at
# https://signalcaptchas.org/registration/generate.html in a browser, right-
# click "Open Signal", paste the link as <token> below.
signal-cli -a +1<gv-number> register --captcha 'signalcaptcha://...'

# Wait for the SMS code to arrive at the GV inbox, then verify:
signal-cli -a +1<gv-number> verify <6-digit-code>

# Set the bot's display name so messages are recognizable:
signal-cli -a +1<gv-number> updateProfile --given-name "Magic Table" --family-name "Finder"

# Smoke test:
signal-cli -a +1<gv-number> send -m "test" +1<your-signal-number>
```

If a recipient's Signal account hides their phone number (`getUserStatus` returns `false`), have them message the bot's Signal first, then run `signal-cli -a +1<gv-number> receive` to learn their UUID. Use `signal:<uuid>` instead of `signal:+phone` in `WATCH_USERS`.

Set `SIGNAL_BOT_NUMBER=+1<gv-number>` in `/opt/disney-dining-bot/.env`.

## 4. Install Timer

```bash
sudo useradd --system --home /opt/disney-dining-bot --shell /usr/sbin/nologin disneybot || true
sudo chown -R disneybot:disneybot /opt/disney-dining-bot
sudo cp deploy/disney-dining-bot.service /etc/systemd/system/
sudo cp deploy/disney-dining-bot.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now disney-dining-bot.timer
```

## 5. Check Health

```bash
journalctl -u disney-dining-bot.service -n 100
curl -H "X-User-Id: craig" -H "X-API-Secret: <craig-password>" https://magictablefinder.com/_api/status
python3 scripts/smoke_test_api.py --user-id craig
python3 scripts/smoke_test_api.py --user-id Jessica
```

If `session_status` is `needs_attention`, run `seed_disney_session.py` again.

For full diagnostic patterns (what each error message means and how to fix it), see [TROUBLESHOOTING.md](TROUBLESHOOTING.md). When the bot can't auto-recover, the admin owner receives a Signal alert within ~10 minutes telling them a manual re-seed is needed; the most recent automated recovery attempt is captured in full at `/var/log/disney-dining-bot/last-recovery.log`.

## 5a. External Watchdog (UptimeRobot)

The in-bot Signal alert can only fire when the bot is running. If the VPS itself dies or the systemd timer stops, that alert is silent. UptimeRobot is the external belt:

- **Public health endpoint:** `GET https://magictablefinder.com/_api/health` (no auth). Returns `200 {"ok":true,"age_minutes":N}` if the bot polled within the last `HEALTH_STALE_MINUTES` (default 30); `503` otherwise.
- **Recommended monitor:** HTTP(s), 5-min interval, alert email to the on-call admin.
- **Tune threshold:** set `HEALTH_STALE_MINUTES` in Netlify env vars.

## 6. Dedicated Disney Login

For a dedicated bot-only MyDisney account, store credentials only in the VPS
`.env` file. You can push them securely from macOS:

```bash
pbpaste | python3 scripts/sync_disney_login_to_vps.py --password-stdin --run-seed-and-poll
```

If Disney presents MFA, CAPTCHA, or passkey checks, automated login will fail. Push credentials without polling:

```bash
pbpaste | python3 scripts/sync_disney_login_to_vps.py --password-stdin
```

Then SSH with a TTY and run the seeder so you can interact with the browser:

```bash
ssh -t -i ~/.ssh/disney_dining_vps root@${DISNEY_VPS_HOST} 'cd /opt/disney-dining-bot && . .venv/bin/activate && DISNEY_HEADLESS=false xvfb-run -a python3 seed_disney_session.py'
```

The script attempts the login, navigates to a dining page, and verifies the
Disney auth cookie via `_fill_first_any_frame`.

Autobook party resolution also requires a protected Friends & Family capture.
Keep it outside the Git checkout because it contains private account and guest
data. Before updating a deployment that previously stored `friends_api_data.json`
in the repository, move that file into root-only runtime storage and add this
path to the VPS `.env`:

```bash
install -d -m 0700 /var/lib/disney-dining-bot
install -m 0600 /opt/disney-dining-bot/friends_api_data.json \
  /var/lib/disney-dining-bot/friends_api_data.json
printf '%s\n' 'DISNEY_FRIENDS_DATA_PATH=/var/lib/disney-dining-bot/friends_api_data.json' >> /opt/disney-dining-bot/.env
```

If there is no existing capture, regenerate it interactively after setting
`DISNEY_FRIENDS_DATA_PATH` in the environment:

```bash
cd /opt/disney-dining-bot
. .venv/bin/activate
set -a; . ./.env; set +a
xvfb-run -a python3 scripts/inspect_friends_headed.py
```

Verify the capture is outside the checkout, owned by the service account, and
readable only by that account before enabling autobook:

```bash
case "$DISNEY_FRIENDS_DATA_PATH" in /opt/disney-dining-bot/*) exit 1;; esac
test "$(stat -c '%U' "$DISNEY_FRIENDS_DATA_PATH")" = root
test "$(stat -c '%a' "$DISNEY_FRIENDS_DATA_PATH")" = 600
python3 -c 'import autobook_service; print(len(autobook_service.get_party_roster()))'
```

## 7. Alert State Safety

The worker alerts only for slots that are open now but were absent in the previous poll. That baseline lives in `open_slots.json`.

Important:

- Do not delete or clear `open_slots.json` casually. If it is missing, the worker baselines current openings without alerting.
- `seen_slots.json` is alert history/audit, not the primary dedupe mechanism.
- The stable dedupe key excludes Disney `offerId`, because Disney can rotate `offerId` for the same visible time.
- If changing alert semantics, stop the timer first:

```bash
sudo systemctl stop disney-dining-bot.timer
# deploy/test/baseline carefully
sudo systemctl start disney-dining-bot.timer
```

Manual worker polls must use Xvfb:

```bash
cd /opt/disney-dining-bot
. .venv/bin/activate
xvfb-run -a python3 disney_bot.py --once
```
