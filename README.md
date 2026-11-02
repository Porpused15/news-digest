# news-digest

Every morning: pulls Canadian news RSS feeds, has Gemini pick GTA / Ontario / Canadian
politics and public-interest stories and summarize each in 2-3 sentences, then emails
the digest grouped by outlet.

## Setup

Add these repo secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | Key from Google AI Studio |
| `SMTP_USER` | Sending Gmail address (dedicated account) |
| `SMTP_PASS` | Gmail App Password for that account (needs 2-step verification) |
| `MAIL_TO` | Recipient address(es), comma-separated |

Test with Actions → Daily news digest → Run workflow.

## Settings

All settings live in `config.json` (edited by the companion app, or by hand):

- `send_hour`: hour of day, Toronto time (0-23). The workflow checks hourly and sends once a day
  at the first run at or after this hour.
- `summary_language`, `lookback_hours`, `email_enabled`
- `feeds`: outlet name -> list of RSS URLs. Feeds that fail are listed in the email footer.

Each run also saves `digests/<date>.json` (kept 90 days) and `digests/index.json`.
A manual run (Actions -> Run workflow, or "Send digest now" in the app) always sends, and counts
as that day's digest.

## Companion app (DroidScript)

1. In DroidScript, create a new **Hybrid** app and replace its code with `app/NewsDigest.js`.
2. Reading digests needs nothing. To change settings from the app, create a fine-grained token
   (GitHub -> Settings -> Developer settings -> Fine-grained tokens) with access to **only this
   repo** and permissions **Contents: Read and write** and **Actions: Read and write**, then paste
   it in the app's Settings tab.
