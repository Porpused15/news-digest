#!/usr/bin/env python3
"""Daily Canadian news digest: RSS feeds -> Gemini (filter + summarize) -> email.

Env vars (required):
  GEMINI_API_KEY   Google AI Studio key
  SMTP_USER        sender login (e.g. Gmail address)
  SMTP_PASS        SMTP password (Gmail: an App Password, not the account password)
  MAIL_TO          recipient(s), comma-separated

Env vars (optional):
  GEMINI_MODEL     default "gemini-flash-latest"
  SMTP_HOST        default "smtp.gmail.com"
  SMTP_PORT        default 465 (SSL)
  MAIL_FROM        default SMTP_USER
  LOOKBACK_HOURS   default 24
  SUMMARY_LANGUAGE default "English" (French sources get summarized into this)
  DRY_RUN          "1" = write digest.html locally instead of sending
"""
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
from google import genai
from google.genai import types
from pydantic import BaseModel


def gnews(site: str, lang: str = "en") -> str:
    """Google News RSS fallback for outlets without a confirmed native feed."""
    if lang == "fr":
        return f"https://news.google.com/rss/search?q=site:{site}+when:1d&hl=fr-CA&gl=CA&ceid=CA:fr"
    return f"https://news.google.com/rss/search?q=site:{site}+when:1d&hl=en-CA&gl=CA&ceid=CA:en"


# Outlet -> feed URLs. Order here = order in the email.
# Feeds marked VERIFY were not confirmed; failures are listed in the email footer.
FEEDS = {
    "The Globe and Mail": [
        "https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/canada/",    # VERIFY
        "https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/politics/",  # VERIFY
    ],
    "Toronto Star": [
        gnews("thestar.com"),  # swap for native thestar.com/search/?f=rss&... feeds once confirmed
    ],
    "The Local": [
        "https://thelocal.to/feed/",
    ],
    "CTV News": [
        gnews("ctvnews.ca"),
    ],
    "CityNews": [
        "https://toronto.citynews.ca/feed/",
    ],
    "CBC News": [
        "https://www.cbc.ca/webfeed/rss/rss-canada",
        "https://www.cbc.ca/webfeed/rss/rss-politics",
        "https://www.cbc.ca/webfeed/rss/rss-canada-toronto",
    ],
    "La Presse": [
        "https://www.lapresse.ca/actualites/politique/rss",
        "https://www.lapresse.ca/actualites/national/rss",
    ],
    "Radio-Canada": [
        gnews("ici.radio-canada.ca", lang="fr"),
    ],
}

USER_AGENT = "Mozilla/5.0 (compatible; daily-news-digest/1.0)"
MAX_DESC_CHARS = 600

PROMPT = """You are preparing a morning news digest.

Below is a JSON list of articles from Canadian news outlets published in the last day.
Select ONLY articles about GTA (Greater Toronto Area), Ontario, or Canadian politics, or
issues of public interest in those places (housing, transit, health care, education,
labour, policing, justice, environment, economy, public policy, etc.).
Exclude sports, entertainment, celebrity, lifestyle, weather-only, purely international
stories with no Canadian angle, and sponsored content.

For each selected article, write a 2-3 sentence factual summary in {language}, based only
on the title and description given. Do not invent details that are not in the input.
If the description is too thin for 2 sentences, write 1.

Return the article "id" and your "summary" for each selected article.

Articles:
{articles}
"""


class Pick(BaseModel):
    id: int
    summary: str


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def entry_time(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_items(lookback_hours: int):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    items, errors, seen_links, seen_titles = [], [], set(), set()

    for outlet, urls in FEEDS.items():
        for url in urls:
            try:
                feed = feedparser.parse(url, agent=USER_AGENT)
            except Exception as exc:  # network-level failure
                errors.append(f"{outlet}: {url} ({exc})")
                continue
            status = getattr(feed, "status", None)
            if (status and status >= 400) or (feed.bozo and not feed.entries):
                errors.append(f"{outlet}: {url} (status {status}, {getattr(feed, 'bozo_exception', '')})")
                continue
            if not feed.entries:
                errors.append(f"{outlet}: {url} (no entries)")
                continue

            for e in feed.entries:
                published = entry_time(e)
                if published and published < cutoff:
                    continue
                link = e.get("link", "").strip()
                title = clean(e.get("title", ""))
                if "news.google.com" in url:
                    # Google News appends " - Publisher" to titles
                    title = re.sub(r"\s+-\s+[^-]+$", "", title)
                key_title = (outlet, title.lower())
                if not link or link in seen_links or key_title in seen_titles:
                    continue
                seen_links.add(link)
                seen_titles.add(key_title)
                items.append({
                    "outlet": outlet,
                    "title": title,
                    "link": link,
                    "description": clean(e.get("summary", ""))[:MAX_DESC_CHARS],
                })
    return items, errors


def summarize(items, model: str, language: str):
    if not items:
        return []
    payload = [
        {"id": i, "outlet": it["outlet"], "title": it["title"], "description": it["description"]}
        for i, it in enumerate(items)
    ]
    prompt = PROMPT.format(language=language, articles=json.dumps(payload, ensure_ascii=False))
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=list[Pick],
        temperature=0.2,
    )

    last_exc = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            picks = resp.parsed
            if picks is None:
                picks = [Pick(**p) for p in json.loads(resp.text)]
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(10 * (attempt + 1))
    else:
        raise RuntimeError(f"Gemini call failed after 3 attempts: {last_exc}")

    selected = []
    for p in picks:
        if 0 <= p.id < len(items):
            selected.append({**items[p.id], "summary": p.summary.strip()})
    return selected


def render(selected, errors, total_scanned, date_str):
    by_outlet = {o: [] for o in FEEDS}
    for it in selected:
        by_outlet[it["outlet"]].append(it)

    h = [f"<h1 style='font-family:sans-serif'>News digest — {html.escape(date_str)}</h1>"]
    t = [f"News digest — {date_str}", ""]
    if not selected:
        h.append("<p>No relevant articles found today.</p>")
        t.append("No relevant articles found today.")
    for outlet, arts in by_outlet.items():
        if not arts:
            continue
        h.append(f"<h2 style='font-family:sans-serif;border-bottom:1px solid #ccc'>{html.escape(outlet)}</h2><ul>")
        t.append(f"== {outlet} ==")
        for a in arts:
            h.append(
                f"<li style='margin-bottom:12px;font-family:sans-serif'>"
                f"<a href='{html.escape(a['link'], quote=True)}'><b>{html.escape(a['title'])}</b></a><br>"
                f"{html.escape(a['summary'])}</li>"
            )
            t += [a["title"], a["summary"], a["link"], ""]
        h.append("</ul>")

    footer = f"{len(selected)} selected from {total_scanned} articles scanned."
    h.append(f"<hr><p style='color:#777;font-size:12px;font-family:sans-serif'>{footer}")
    t += ["--", footer]
    if errors:
        h.append("<br>Feeds that failed:<br>" + "<br>".join(html.escape(e) for e in errors))
        t += ["Feeds that failed:"] + errors
    h.append("</p>")
    return "\n".join(h), "\n".join(t)


def send(subject, html_body, text_body):
    user = os.environ["SMTP_USER"]
    to = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM", user)
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(user, os.environ["SMTP_PASS"])
        s.sendmail(msg["From"], to, msg.as_string())


def main():
    lookback = int(os.environ.get("LOOKBACK_HOURS", "24"))
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    language = os.environ.get("SUMMARY_LANGUAGE", "English")
    date_str = datetime.now().strftime("%A, %B %d, %Y")

    items, errors = fetch_items(lookback)
    print(f"Fetched {len(items)} items; {len(errors)} feed errors", file=sys.stderr)
    selected = summarize(items, model, language)
    html_body, text_body = render(selected, errors, len(items), date_str)

    if os.environ.get("DRY_RUN") == "1":
        with open("digest.html", "w", encoding="utf-8") as f:
            f.write(html_body)
        print(text_body)
        return
    send(f"News digest — {date_str}", html_body, text_body)
    print(f"Sent: {len(selected)} articles", file=sys.stderr)


if __name__ == "__main__":
    main()
