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

## Notes

- Runs at 10:00 UTC (6:00 Toronto in summer, 5:00 in winter). Edit the cron in
  `.github/workflows/digest.yml` to change it.
- Feeds live in `FEEDS` at the top of `news_digest.py`. Feeds that fail are listed in
  the email footer.
- Optional env vars: `GEMINI_MODEL`, `SUMMARY_LANGUAGE`, `LOOKBACK_HOURS` (see script header).
