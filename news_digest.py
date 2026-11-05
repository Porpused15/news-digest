#!/usr/bin/env python3
"""Daily Canadian news digest: RSS feeds -> Gemini (filter + summarize) -> email + JSON.

Settings (feeds, send hour, language, lookback, email on/off) live in config.json,
which the companion app edits. Each run writes digests/<date>.json and digests/index.json.

Env vars (required):
  GEMINI_API_KEY   Google AI Studio key
  SMTP_USER        sender login (e.g. Gmail address)
  SMTP_PASS        SMTP password (Gmail: an App Password, not the account password)
  MAIL_TO          recipient(s), comma-separated

Env vars (optional):
  GEMINI_MODEL     main model (defaults below depend on backend)
  GEMINI_FALLBACK_MODEL  lighter model tried after two main-model failures

Backend: Vertex AI when GOOGLE_GENAI_USE_VERTEXAI=true (project, location and service
account credentials come from GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION /
GOOGLE_APPLICATION_CREDENTIALS, as exported by google-github-actions/auth); otherwise the
Gemini API (AI Studio) with GEMINI_API_KEY.
  CONFIG_PATH      default "config.json"
  SMTP_HOST        default "smtp.gmail.com"
  SMTP_PORT        default 465 (SSL)
  MAIL_FROM        default SMTP_USER
  DRY_RUN          "1" = write digest.html locally instead of sending email
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
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
from google import genai
from google.genai import types
from typing import Literal

from pydantic import BaseModel


TZ = ZoneInfo("America/Toronto")
DIGEST_DIR = Path("digests")
KEEP_DAYS = 90


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("summary_language", "English")
    if isinstance(cfg["summary_language"], list):  # tolerate list values from older app builds
        cfg["summary_language"] = (cfg["summary_language"] or ["English"])[0]
    cfg.setdefault("lookback_hours", 24)
    cfg.setdefault("email_enabled", True)
    if cfg.get("email_group_by") not in ("newspaper", "topic"):
        cfg["email_group_by"] = "newspaper"
    if not cfg.get("feeds"):
        raise SystemExit("config.json has no feeds")
    return cfg


VERTEX = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true"
# Vertex serves pinned model versions; AI Studio keeps the floating aliases.
MAIN_MODEL = os.environ.get("GEMINI_MODEL") or ("gemini-3.5-flash" if VERTEX else "gemini-flash-latest")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL") or (
    "gemini-3.5-flash-lite" if VERTEX else "gemini-flash-lite-latest")
USER_AGENT ="Mozilla/5.0 (compatible; daily-news-digest/1.0)"
MAX_DESC_CHARS = 600

# Display order for the topic view. Gemini must pick one of these per story.
TOPICS = [
    "Federal politics",
    "Ontario politics",
    "Toronto & GTA politics",
    "Economy & trade",
    "Housing",
    "Transit & transportation",
    "Health",
    "Education",
    "Justice & policing",
    "Environment & energy",
    "Labour",
    "Indigenous affairs",
    "Other public interest",
]
Topic = Literal[tuple(TOPICS)]  # type: ignore[valid-type]

# Gemini 3 guidance (ai.google.dev Gemini 3 developer guide / prompting guide): data first,
# instructions after it, negative constraints last, short and direct, default temperature.
PROMPT = """Articles (JSON):
{articles}

The articles above (id, outlet, title, description) are the only source of information. Do not add facts from anywhere else.

Build a morning news briefing for a reader in Toronto who follows politics and public affairs.

1. Select articles about government, politics or public policy at the federal, Ontario or Toronto/GTA level, and issues of public interest in Ontario or Canada: public services (health care, transit, schools, housing), the economy and trade, courts and justice policy, policing as an institution, labour, the environment, Indigenous affairs, and accountability stories about public institutions.
2. Group selected articles that report the same story (the same event, announcement or development), across outlets and languages. Each article belongs to at most one story.
3. For each story give:
   - article_ids: the ids of its articles
   - topic: the single best fit. Politics topics follow the level of government: Federal = Parliament, the federal government and parties, federal-provincial relations; Ontario = Queen's Park and the Ontario government; Toronto & GTA = municipal councils and mayors. Politics in other provinces goes under Federal politics only when it has national significance; otherwise leave it out. Jails, courts and corrections go under Justice & policing.
   - headline: a neutral headline in {language}, at most 15 words
   - summary: 2-3 sentences in {language}: what happened, then why it matters or what comes next, combining details from all of the story's articles
4. Order stories from most to least significant for the public.

Do not select: individual crimes, crashes, fires, missing persons or weather events unless the article is about a policy, a public institution or an official response; sports; entertainment, arts and celebrity; lifestyle and consumer tips; international news with no Canadian angle; sponsored content. Do not begin a summary with "This article" or name the outlet in it."""


class Story(BaseModel):
    article_ids: list[int]
    topic: Topic
    headline: str
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


HOST_GAP = 5.0      # seconds between requests to the same site (the Star returned 429 even at 2 s)
_last_hit: dict[str, float] = {}


def get_feed(url: str):
    """feedparser.parse with per-host spacing and one retry on HTTP 429."""
    from urllib.parse import urlsplit
    host = urlsplit(url).netloc
    for attempt in range(2):
        wait = HOST_GAP - (time.monotonic() - _last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        feed = feedparser.parse(url, agent=USER_AGENT)
        _last_hit[host] = time.monotonic()
        if getattr(feed, "status", None) != 429 or attempt:
            return feed
        retry_after = (getattr(feed, "headers", {}) or {}).get("retry-after", "")
        time.sleep(min(int(retry_after), 60) if retry_after.isdigit() else 15)
    return feed


def fetch_items(feeds: dict, lookback_hours: int):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    items, errors, seen_links, seen_titles = [], [], set(), set()
    counts = {o: 0 for o in feeds}  # articles each outlet's feeds delivered in the window

    for outlet, urls in feeds.items():
        for url in urls:
            try:
                feed = get_feed(url)
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
                if "news.google.com" in url and " - " in title:
                    # Google News appends " - Publisher"; the name may contain hyphens
                    title = title.rsplit(" - ", 1)[0]
                key_title = (outlet, title.lower())
                if not link or link in seen_links or key_title in seen_titles:
                    continue
                seen_links.add(link)
                seen_titles.add(key_title)
                desc = clean(e.get("summary", ""))
                if desc.startswith(title):  # Google News repeats the headline as the "teaser"
                    desc = ""
                counts[outlet] += 1
                items.append({
                    "outlet": outlet,
                    "title": title,
                    "link": link,
                    "description": desc[:MAX_DESC_CHARS],
                })
    return items, errors, counts


def summarize(items, model: str, language: str):
    """Ask Gemini to select, group into stories, categorize and summarize.

    Returns stories in significance order: {topic, headline, summary, articles: [item, ...]}.
    """
    if not items:
        return []
    payload = [
        {"id": i, "outlet": it["outlet"], "title": it["title"], "description": it["description"]}
        for i, it in enumerate(items)
    ]
    prompt = PROMPT.format(language=language, articles=json.dumps(payload, ensure_ascii=False))
    # Per-request timeout: a hung call previously stalled the job until GitHub's 6-hour kill.
    # The SDK passes this to Google as the server deadline: at 4 minutes the main model
    # hit 504 DEADLINE_EXCEEDED grouping ~330 articles, so allow 7.
    http = types.HttpOptions(timeout=420_000)
    client = (genai.Client(http_options=http) if VERTEX  # project/location/creds from env
              else genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=http))
    # Temperature left at the default (1.0): Google advises against lowering it for Gemini 3.
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=list[Story],
    )
    # Main model: medium thinking (Gemini 3 Flash defaults to high, which ran past the
    # deadline). The fallback keeps its own default.
    main_config = config.model_copy(update={
        "thinking_config": types.ThinkingConfig(thinking_level=types.ThinkingLevel.MEDIUM)})

    # Gemini often returns 503 "high demand" for minutes at a time. The main model gets two
    # tries before the lighter fallback (whose picks are noticeably thinner), then one more.
    # If all fail, the job exits and the next hourly scheduled run retries.
    plan = [(model, 0), (model, 45), (FALLBACK_MODEL, 90), (model, 180)]
    last_exc = None
    for attempt, (m, wait) in enumerate(plan):
        time.sleep(wait)
        try:
            resp = client.models.generate_content(model=m, contents=prompt,
                                                  config=main_config if m == model else config)
            raw = resp.parsed
            if raw is None:
                raw = [Story(**p) for p in json.loads(resp.text)]
            print(f"Gemini answered: {m} via {'Vertex AI' if VERTEX else 'Gemini API'} "
                  f"(attempt {attempt + 1})", file=sys.stderr)
            break
        except Exception as exc:
            last_exc = exc
            print(f"Gemini attempt {attempt + 1} ({m}) failed: {str(exc)[:160]}", file=sys.stderr)
    else:
        raise RuntimeError(f"Gemini call failed after {len(plan)} attempts: {last_exc}")

    stories, used = [], set()
    for st in raw:
        ids = [i for i in dict.fromkeys(st.article_ids) if 0 <= i < len(items) and i not in used]
        if not ids:
            continue
        used.update(ids)
        stories.append({
            "topic": st.topic if st.topic in TOPICS else "Other public interest",
            "headline": st.headline.strip(),
            "summary": st.summary.strip(),
            "articles": [items[i] for i in ids],
        })
    return stories


def by_newspaper(feeds, stories):
    """[(outlet, [(article, story), ...])] in feed order; one article per story per outlet."""
    groups = {o: [] for o in feeds}
    for st in stories:
        seen = set()
        for a in st["articles"]:
            if a["outlet"] not in seen:
                seen.add(a["outlet"])
                groups.setdefault(a["outlet"], []).append((a, st))
    return [(o, g) for o, g in groups.items() if g]


def by_topic(stories):
    """[(topic, [story, ...])] in TOPICS order."""
    groups = {t: [] for t in TOPICS}
    for st in stories:
        groups[st["topic"]].append(st)
    return [(t, g) for t, g in groups.items() if g]


def outlet_counts_line(feeds, stories, counts):
    """'CBC News 45 (6 picked) · ...' in feed order."""
    picked = {o: 0 for o in feeds}
    for st in stories:
        for a in st["articles"]:
            picked[a["outlet"]] = picked.get(a["outlet"], 0) + 1
    return " · ".join(f"{o} {counts.get(o, 0)} ({picked.get(o, 0)} picked)" for o in feeds)


def render(feeds, stories, errors, total_scanned, date_str, group_by, counts):
    esc = html.escape
    h = [f"<h1 style='font-family:sans-serif'>News digest — {esc(date_str)}</h1>"]
    t = [f"News digest — {date_str}", ""]
    if not stories:
        h.append("<p>No relevant articles found today.</p>")
        t.append("No relevant articles found today.")

    def section(name):
        h.append(f"<h2 style='font-family:sans-serif;border-bottom:1px solid #ccc'>{esc(name)}</h2><ul>")
        t.append(f"== {name} ==")

    li = "<li style='margin-bottom:12px;font-family:sans-serif'>"
    if group_by == "topic":
        for topic, sts in by_topic(stories):
            section(topic)
            for st in sts:
                links = " · ".join(f"<a href='{esc(a['link'], quote=True)}'>{esc(a['outlet'])}</a>"
                                   for a in st["articles"])
                h.append(f"{li}<b>{esc(st['headline'])}</b><br>{esc(st['summary'])}<br>"
                         f"<span style='font-size:13px'>{links}</span></li>")
                t += [st["headline"], st["summary"]] + [f"  {a['outlet']}: {a['link']}" for a in st["articles"]] + [""]
            h.append("</ul>")
    else:
        for outlet, pairs in by_newspaper(feeds, stories):
            section(outlet)
            for a, st in pairs:
                h.append(f"{li}<a href='{esc(a['link'], quote=True)}'><b>{esc(a['title'])}</b></a><br>"
                         f"{esc(st['summary'])}</li>")
                t += [a["title"], st["summary"], a["link"], ""]
            h.append("</ul>")

    n_articles = sum(len(st["articles"]) for st in stories)
    footer = f"{len(stories)} stories ({n_articles} articles) selected from {total_scanned} scanned."
    per = "In the feeds: " + outlet_counts_line(feeds, stories, counts)
    h.append(f"<hr><p style='color:#777;font-size:12px;font-family:sans-serif'>{footer}<br>{esc(per)}")
    t += ["--", footer, per]
    if errors:
        h.append("<br>Feeds that failed:<br>" + "<br>".join(esc(e) for e in errors))
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


def save_json(feeds, stories, errors, total_scanned, now, counts):
    """Write digests/<date>.json (format 2: stories) and refresh digests/index.json."""
    DIGEST_DIR.mkdir(exist_ok=True)
    date = now.strftime("%Y-%m-%d")
    digest = {
        "format": 2,
        "date": date,
        "generated_at": now.isoformat(timespec="seconds"),
        "scanned": total_scanned,
        "scanned_by_outlet": {o: counts.get(o, 0) for o in feeds},
        "outlet_order": list(feeds),
        "topic_order": TOPICS,
        "stories": [
            {"topic": st["topic"], "headline": st["headline"], "summary": st["summary"],
             "articles": [{"outlet": a["outlet"], "title": a["title"], "link": a["link"]}
                          for a in st["articles"]]}
            for st in stories
        ],
        "errors": errors,
    }
    (DIGEST_DIR / f"{date}.json").write_text(json.dumps(digest, ensure_ascii=False, indent=1), encoding="utf-8")

    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    dates = []
    for f in DIGEST_DIR.glob("????-??-??.json"):
        if f.stem < cutoff:
            f.unlink()
        else:
            dates.append(f.stem)
    dates.sort(reverse=True)
    (DIGEST_DIR / "index.json").write_text(json.dumps({"dates": dates}, indent=1), encoding="utf-8")


def main():
    cfg = load_config(os.environ.get("CONFIG_PATH", "config.json"))
    model = MAIN_MODEL
    now = datetime.now(TZ)
    date_str = now.strftime("%A, %B %d, %Y")
    feeds = cfg["feeds"]

    items, errors, counts = fetch_items(feeds, int(cfg["lookback_hours"]))
    print(f"Fetched {len(items)} items; {len(errors)} feed errors", file=sys.stderr)
    stories = summarize(items, model, cfg["summary_language"])
    save_json(feeds, stories, errors, len(items), now, counts)
    html_body, text_body = render(feeds, stories, errors, len(items), date_str, cfg["email_group_by"], counts)

    if os.environ.get("DRY_RUN") == "1":
        with open("digest.html", "w", encoding="utf-8") as f:
            f.write(html_body)
        print(text_body)
        return
    if cfg["email_enabled"]:
        send(f"News digest — {date_str}", html_body, text_body)
        print(f"Sent: {len(stories)} stories", file=sys.stderr)
    else:
        print("Email disabled in config; saved JSON only", file=sys.stderr)


if __name__ == "__main__":
    main()
