# news-digest

Every morning: pulls Canadian news RSS feeds, has Gemini pick GTA / Ontario / Canadian
politics and public-interest stories and summarize each in 2-3 sentences, then emails
the digest grouped by outlet.

## Setup

Add these repo secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `GCP_SA_KEY` | Vertex AI service account key (JSON, one line) |
| `GEMINI_API_KEY` | Key from Google AI Studio (used only without `GCP_SA_KEY`) |
| `SMTP_USER` | Sending Gmail address (dedicated account) |
| `SMTP_PASS` | Gmail App Password for that account (needs 2-step verification) |
| `MAIL_TO` | Recipient address(es), comma-separated |

Test with Actions → Daily news digest → Run workflow.

## Gemini backend

- **Vertex AI** (used when the `GCP_SA_KEY` secret exists): service account JSON key with the
  *Vertex AI User* role, Vertex AI API enabled on its project. Location `global`.
- **Gemini API / AI Studio** otherwise, with `GEMINI_API_KEY`.
- Models can be overridden with repository **variables** `GEMINI_MODEL` and
  `GEMINI_FALLBACK_MODEL` (defaults on Vertex: `gemini-3.5-flash`, `gemini-3.5-flash-lite`).
  The run log says which model answered.

## Settings

All settings live in `config.json` (edited by the companion app, or by hand):

- `send_hour`: hour of day, Toronto time (0-23). The workflow checks hourly and sends once a day
  at the first run at or after this hour.
- `summary_language`, `lookback_hours`, `email_enabled`, `email_group_by` (`newspaper` or `topic`)
- `feeds`: outlet name -> list of RSS URLs. Feeds that fail are listed in the email footer.

Each run also saves `digests/<date>.json` (kept 90 days) and `digests/index.json`.
A manual run (Actions -> Run workflow, or "Send digest now" in the app) always sends, and counts
as that day's digest.

## Companion app (DroidScript)

1. In DroidScript, create a new **Hybrid** app and replace its code with `app/NewsDigest.js`.
2. Reading digests needs nothing. To change settings from the app, create a fine-grained token
   (GitHub -> Settings -> Developer settings -> Fine-grained tokens) with access to **only this
   repo** and permissions **Contents: Read and write** and **Actions: Read and write**, then paste
   it into the app's Settings tab (it saves automatically and shows whether it works or has expired).
