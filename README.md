# news-digest

Every morning: pulls Canadian news RSS feeds, has Gemini pick GTA / Ontario / Canadian
politics and public-interest stories and summarize each in 2-3 sentences, then emails
the digest grouped by outlet.

## Setup

Add these repo secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `SMTP_USER` | Sending Gmail address (dedicated account) |
| `SMTP_PASS` | Gmail App Password for that account (needs 2-step verification) |
| `MAIL_TO` | Recipient address(es), comma-separated |

Test with Actions → Daily news digest → Run workflow.

## Gemini backend

- **Gemini Enterprise Agent Platform** (formerly Vertex AI), location `global`. GitHub logs in
  keylessly via Workload Identity Federation (pool `github`, provider `news-digest`, restricted
  to this repo) as service account `news-digest@...` with role `roles/aiplatform.user`.
- The script can also use the Gemini API (AI Studio) with `GEMINI_API_KEY` when
  `GOOGLE_GENAI_USE_ENTERPRISE` isn't `true` (not used by the workflow).
- Models can be overridden with repository **variables** `GEMINI_MODEL` and
  `GEMINI_FALLBACK_MODEL` (defaults on Agent Platform: `gemini-3.8-flash`, fallback `gemini-3.5-flash`).
  The run log says which model answered.

## Settings

All settings live in `config.json` in the separate public repo
[news-digest-config](https://github.com/Porpused15/news-digest-config), which the workflow
checks out at the start of each run. The companion app's token only has access to that repo,
so it can change settings but not this code (which runs with the email and Google secrets).

- `send_hour`: hour of day, Toronto time (0-23). The workflow checks hourly and sends once a day
  at the first run at or after this hour.
- `summary_language`, `lookback_hours`, `email_enabled`, `email_group_by` (`level` (default), `newspaper` or `topic`)
- `feeds`: outlet name -> list of RSS URLs. Feeds that fail are listed in the email footer.

Each run also saves `digests/<date>.json` (kept 90 days) and `digests/index.json`.
A manual run (Actions -> Run workflow, or "Send digest now" in the app) always sends, and counts
as that day's digest.

## Companion app (DroidScript)

1. In DroidScript, create a new **Hybrid** app and replace its code with `app/NewsDigest.js`.
   Add a second file named exactly **`Service.js`** to the same project with the contents of
   `app/Service.js`: the background check that posts the morning "today's digest" notification
   (from 6:40, then every 30 min until noon if the digest is late; once a day; nothing if the
   app already showed today's digest). The "Morning notification" switch in Settings turns it
   off per phone. If notifications stop on a phone, set the app's battery use to Unrestricted.
   After a reboot the check resumes the next time the app is opened.
2. Reading digests needs nothing. To change settings or use "Send digest now", create a
   fine-grained token (GitHub -> Settings -> Developer settings -> Fine-grained tokens) with access
   to **only `news-digest-config`** and permissions **Contents: Read and write** and **Actions:
   Read and write**, then paste it into the app's Settings tab. Don't give it access to this repo.
   "Send digest now" runs a relay workflow in `news-digest-config`, which starts this repo's
   digest workflow with its own Actions-only token (see that repo's README).
