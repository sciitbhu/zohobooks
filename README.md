# Zoho Books × Claude Financial Intelligence Engine

A self-hosted, macOS-native automation system with two independent tracks:

- **Track 1 — Real-time income categorization**: a FastAPI webhook that
  listens for Zoho Books invoice events, asks Claude to classify the income
  (`B2B` / `B2C` / `Overseas` / `Custom_Sector`), and writes the result back
  to a Zoho Books custom field.
- **Track 2 — Automated EOM MIS report**: a `launchd`-scheduled script that
  pulls last month's P&L, balance sheet, expenses, inventory, fixed assets,
  and bills from Zoho Books, has Claude draft an executive Markdown report,
  saves it locally, and creates macOS Calendar reminders for every
  pending/overdue bill.

## 1. Project structure

```
zoho_claude_intelligence/
├── .env.example              # Template — copy to .env and fill in secrets
├── .gitignore
├── requirements.txt
├── config.py                 # Centralized, validated settings (pydantic)
├── logging_setup.py          # Shared console + rotating file logging
├── clients/
│   ├── zoho_client.py        # Zoho Books OAuth2 + API wrapper
│   └── claude_client.py      # Anthropic API wrapper (classify + report gen)
├── prompts/
│   ├── categorization_prompt.py   # Track 1 prompt template
│   └── mis_report_prompt.py       # Track 2 prompt template
├── webhook/
│   └── server.py              # Track 1: FastAPI webhook listener
├── mis/
│   ├── generate_mis_report.py # Track 2: main orchestration script
│   └── calendar_sync.py       # macOS Calendar event creation (AppleScript)
├── scripts/
│   └── zoho_oauth_bootstrap.py # ONE-TIME script to obtain refresh token
├── launchd/
│   └── com.evalzero.zoho.plist  # macOS scheduler config
└── logs/                      # Rotating app logs + launchd stdout/stderr
```

## 2. Environment & dependency setup (macOS)

```bash
cd zoho_claude_intelligence

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy the env template and fill in real values
cp .env.example .env
```

Edit `.env` and fill in:
- `ANTHROPIC_API_KEY` — from https://console.anthropic.com
- `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` — from https://api-console.zoho.com
- `ZOHO_ORGANIZATION_ID` — visible in Zoho Books under Settings → Organization
- `ZOHO_API_DOMAIN` / `ZOHO_ACCOUNTS_DOMAIN` — match your Zoho data center
- `ZOHO_WEBHOOK_SHARED_SECRET` — any long random string you make up
- `ZOHO_INVOICE_CUSTOM_FIELD_API_NAME` — create this custom field once in
  Zoho Books: Settings → Customization → Invoices → Custom Fields (a simple
  "Text" or "Dropdown" field works; note its **API name**, not its label)

`ZOHO_REFRESH_TOKEN` is obtained via the one-time bootstrap step below.

## 3. Zoho OAuth bootstrap (one-time)

Zoho's refresh token can only be obtained via an interactive browser
consent flow the first time:

```bash
# Edit CLIENT_ID / CLIENT_SECRET / ACCOUNTS_DOMAIN at the top of the file first
python scripts/zoho_oauth_bootstrap.py
```

This opens your browser, you approve access, and the script prints a
`ZOHO_REFRESH_TOKEN` value — paste it into `.env`. `ZohoBooksClient` handles
all future access-token refreshes automatically from then on; you should
not need to run this again unless you revoke the app or rotate secrets.

## 4. Running Track 1 — the webhook server

```bash
source venv/bin/activate
uvicorn webhook.server:app --host 127.0.0.1 --port 8787
```

Verify it's alive:
```bash
curl http://127.0.0.1:8787/healthz
# {"status": "ok"}
```

**Exposing it to Zoho:** Zoho Books needs to reach this endpoint over the
public internet. From a Mac, the simplest safe option is a tunnel — e.g.
Cloudflare Tunnel or `ngrok http 8787` — rather than opening a port on your
router. Point the Zoho Books webhook (Settings → Automation → Webhooks →
New Webhook) at:

```
https://<your-tunnel-hostname>/webhook/zoho/invoice?secret=<ZOHO_WEBHOOK_SHARED_SECRET>
```

Trigger it on **Invoice Created** and/or **Payment Received**, matching
whichever event you want to drive categorization.

To keep the webhook server itself running persistently in the background
(separately from the monthly `launchd` job below), the simplest option is a
second, always-on `launchd` **agent** (not the calendar-triggered one in
`launchd/`) with `KeepAlive: true` instead of `StartCalendarInterval`. Ask
if you'd like that second plist — it's a small variation on the one below.

## 5. Running Track 2 — the MIS report manually (test before scheduling)

```bash
source venv/bin/activate
python -m mis.generate_mis_report
```

This generates the report for **last month** relative to today. To backfill
or test a specific month:

```bash
python -m mis.generate_mis_report --year 2026 --month 6
```

Output:
- Markdown report: `~/Documents/Zoho_MIS_Reports/2026/06_Report.md`
- Calendar events created in a macOS Calendar named **"Zoho Bills &
  Payments"** (auto-created if it doesn't exist) for every pending/overdue
  bill, with a display alarm 9 hours before the 09:00 due-date reminder.

The first time this runs, macOS will prompt you to grant Terminal (or
whichever app launched Python) permission to control **Calendar** — approve
it, or the AppleScript calls will silently fail. You can check/grant this
manually under **System Settings → Privacy & Security → Automation**.

## 6. Installing the `launchd` job (scheduled monthly run)

```bash
# 1. Fill in real paths in the plist (replace YOUR_USERNAME + confirm the
#    project path matches where you actually cloned/placed this folder)
sed -i '' "s|/Users/YOUR_USERNAME/zoho_claude_intelligence|$(pwd)|g" \
    launchd/com.evalzero.zoho.plist

# 2. Copy it into your user LaunchAgents directory
mkdir -p ~/Library/LaunchAgents
cp launchd/com.evalzero.zoho.plist ~/Library/LaunchAgents/

# 3. Load it
launchctl load ~/Library/LaunchAgents/com.evalzero.zoho.plist

# 4. Verify it's registered
launchctl list | grep zohomis
```

**To test immediately** without waiting for the 1st of the month, temporarily
flip `RunAtLoad` to `<true/>` in the plist, reload it (`launchctl unload`
then `launchctl load`), check `logs/launchd_stdout.log` /
`logs/launchd_stderr.log`, then set it back to `<false/>` and reload again.

**To uninstall:**
```bash
launchctl unload ~/Library/LaunchAgents/com.evalzero.zoho.plist
rm ~/Library/LaunchAgents/com.evalzero.zoho.plist
```

**To change the schedule** (e.g. a different day/time), edit the
`StartCalendarInterval` block in the plist, then `unload` + `load` again —
launchd does not hot-reload a running plist's schedule.

## 7. Monitoring & troubleshooting

- **App-level logs** (both tracks): `logs/app_YYYY-MM-DD.log` — rotates
  daily, retained 14 days, compressed after rotation.
- **launchd execution logs**: `logs/launchd_stdout.log` and
  `logs/launchd_stderr.log` — check these first if the monthly job didn't
  seem to run at all (vs. running and failing, which shows in `app_*.log`).
- **Common failure: 401 from Zoho** — usually means the refresh token was
  revoked (e.g. you changed the app's scopes in api-console.zoho.com).
  Re-run `scripts/zoho_oauth_bootstrap.py` and update `.env`.
- **Common failure: Calendar events not appearing** — check macOS
  Automation permissions (Section 5) and confirm Calendar.app is not
  restricted by Screen Time/parental controls on this machine.
- **Common failure: `/fixedassets` 400/404** — Fixed Asset Management is a
  paid add-on module in some Zoho Books plans; if you don't have it
  enabled, `generate_mis_report.py` already degrades gracefully and the
  report will just state fixed asset data wasn't available.

## 8. Extending this system

- **Swap the Claude model**: change `CLAUDE_MODEL` in `.env` — nothing else
  needs to change, since `clients/claude_client.py` reads it from
  `settings.claude_model`.
- **Add more MIS data sources**: add a method to `ZohoBooksClient`, wire it
  into `_gather_financial_data()` in `mis/generate_mis_report.py`, and
  mention the new section in `prompts/mis_report_prompt.py`'s required
  section ordering.
- **Change the calendar reminder timing**: adjust `alarm_minutes_before` in
  `mis/calendar_sync.py`.
- **Narrow Zoho OAuth scopes**: the bootstrap script currently requests
  `ZohoBooks.fullaccess.all` for simplicity — for least-privilege in
  production, swap in the specific scopes you actually use (invoices,
  contacts, expenses, reports, bills, settings) per Zoho's OAuth scope docs
  (https://www.zoho.com/books/api/v3/oauth/).
