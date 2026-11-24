#!/usr/bin/env python3
"""Daily Canadian news digest: RSS feeds -> Gemini (filter + summarize) -> email + JSON.

Settings (feeds, send hour, language, lookback, email on/off) live in config.json in the
separate Porpused15/news-digest-config repo (checked out to settings/ by the workflow),
which the companion app edits. Each run writes digests/<date>.json and digests/index.json.

Env vars (required):
  GEMINI_API_KEY   Google AI Studio key
  SMTP_USER        sender login (e.g. Gmail address)
  SMTP_PASS        SMTP password (Gmail: an App Password, not the account password)
  MAIL_TO          recipient(s), comma-separated

Env vars (optional):
  GEMINI_MODEL     main model (defaults below depend on backend)
  GEMINI_FALLBACK_MODEL  lighter model tried after two main-model failures

Backend: Gemini Enterprise Agent Platform (formerly Vertex AI) when
GOOGLE_GENAI_USE_ENTERPRISE=true (project, location and service
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
    if cfg.get("email_group_by") not in GROUPINGS:
        cfg["email_group_by"] = "level"
    if not cfg.get("feeds"):
        raise SystemExit("config.json has no feeds")
    return cfg


# Gemini Enterprise Agent Platform (formerly Vertex AI). The SDK reads the same variable.
ENTERPRISE = os.environ.get("GOOGLE_GENAI_USE_ENTERPRISE", "").lower() == "true"
# The platform serves pinned model versions; AI Studio keeps the floating aliases.
# Both confirmed answering on the project's global endpoint (2026-11-05).
MAIN_MODEL = os.environ.get("GEMINI_MODEL") or ("gemini-3.8-flash" if ENTERPRISE else "gemini-flash-latest")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL") or (
    "gemini-3.5-flash" if ENTERPRISE else "gemini-flash-lite-latest")
USER_AGENT ="Mozilla/5.0 (compatible; daily-news-digest/1.0)"
MAX_DESC_CHARS = 600

GROUPINGS = ("level", "newspaper", "topic")

# Level of coverage, in display order. Gemini must pick one per story.
LEVELS = ["International", "National", "Ontario", "Greater Toronto Area"]
Level = Literal[tuple(LEVELS)]  # type: ignore[valid-type]

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

The articles above (id, outlet, author, title, description) are the only source of information. Do not add facts from anywhere else. Some articles have only a title and no description: use nothing beyond what the title states.

Build a morning news briefing for a reader in Toronto who follows politics and public affairs.

1. Select articles about government, politics or public policy at the federal, Ontario or Toronto/GTA level, and issues of public interest in Ontario or Canada: public services (health care, transit, schools, housing), the economy and trade, courts and justice policy, policing as an institution, labour, the environment, Indigenous affairs, accountability stories about public institutions, and Canada's dealings with other countries.
2. Group selected articles about the same story or the same specific issue (for example, a bill and the reactions to it) under one story, across outlets and languages. Each article belongs to at most one story. If two articles are essentially the same report (for example, the same wire story), include only one of them.
3. For each story give:
   - article_ids: the ids of its articles
   - level: International = Canada's relations with other countries (trade, diplomacy, defence, summits) or international events that directly involve Canada or Canadians; National = federal politics and national issues; Ontario = provincial politics and Ontario-wide issues; Greater Toronto Area = municipal politics and local issues in Toronto and the GTA
   - topic: the single best fit. Politics topics follow the level of government: Federal = Parliament, the federal government and parties, federal-provincial relations; Ontario = Queen's Park and the Ontario government; Toronto & GTA = municipal councils and mayors. Politics in other provinces goes under Federal politics only when it has national significance; otherwise leave it out. Jails, courts and corrections go under Justice & policing.
   - headline: a neutral headline in {language}, at most 15 words, in sentence case (capitalize only the first word and proper nouns)
   - summary: 2-3 sentences in {language}: what happened, then why it matters or what comes next, combining details from all of the story's articles
   - notes: when the story has more than one article, one short phrase in {language} per article (by id) saying what that article adds or how it differs from the others (its angle, focus or new detail); an empty list when the story has one article
4. Order stories from most to least significant for the public.

Do not select: local news (municipal politics or local issues) from outside the Greater Toronto Area, such as Ottawa, Hamilton, London, Waterloo Region or Sudbury, unless it has provincial or national significance; shootings, assaults, driving accidents, fires or other incidents unless they matter beyond the community where they happened or the article is about a policy, a public institution or an official response; missing persons and weather events on the same terms; international news with no Canadian angle; sports; entertainment, arts and celebrity; lifestyle and consumer tips; sponsored content. Do not begin a summary or note with "This article" or name the outlet in it."""


class Note(BaseModel):
    id: int
    angle: str


class Story(BaseModel):
    article_ids: list[int]
    level: Level
    topic: Topic
    headline: str
    summary: str
    notes: list[Note]


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_author(raw: str) -> str:
    """'jane@x.com (Jane Doe)' -> 'Jane Doe'; drops bare emails and a leading 'By'."""
    a = clean(raw)
    m = re.search(r"\(([^)]+)\)", a)
    if m and "@" in a:
        a = m.group(1)
    a = re.sub(r"\S+@\S+", "", a)
    a = re.sub(r"^by\s+", "", a, flags=re.I)
    return re.sub(r"\s+", " ", a).strip(" ,;")


def title_key(title: str) -> str:
    return " ".join(re.findall(r"\w+", title.lower()))


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
    items, errors, seen_links, seen_titles = [], [], set(), {}
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
                if not link or link in seen_links:
                    continue
                seen_links.add(link)
                counts[outlet] += 1
                # Same headline already seen = same report (often one wire story run by
                # several outlets): keep the first copy and note where else it ran.
                key = title_key(title)
                if key in seen_titles and len(key.split()) >= 4:
                    first = items[seen_titles[key]]
                    if outlet != first["outlet"] and outlet not in first["also_in"]:
                        first["also_in"].append(outlet)
                    continue
                # Google News "descriptions" are only the headline again (sometimes with
                # punctuation differences), never a teaser: treat its items as title-only.
                desc = "" if "news.google.com" in url else clean(e.get("summary", ""))
                seen_titles[key] = len(items)
                items.append({
                    "outlet": outlet,
                    "author": clean_author(e.get("author", "")),
                    "title": title,
                    "link": link,
                    "description": desc[:MAX_DESC_CHARS],
                    "also_in": [],
                })
    return items, errors, counts


THINKING = types.ThinkingLevel.MEDIUM  # main model only; compare_prompts.py varies it


def summarize(items, model: str, language: str):
    """Ask Gemini to select, group into stories, categorize and summarize.

    Returns stories in significance order: {topic, headline, summary, articles: [item, ...]}.
    """
    if not items:
        return []
    payload = [
        {"id": i, "outlet": it["outlet"], "author": it["author"], "title": it["title"],
         "description": it["description"]}
        for i, it in enumerate(items)
    ]
    prompt = PROMPT.format(language=language, articles=json.dumps(payload, ensure_ascii=False))
    # Per-request timeout: a hung call previously stalled the job until GitHub's 6-hour kill.
    # The SDK passes this to Google as the server deadline: at 4 minutes the main model
    # hit 504 DEADLINE_EXCEEDED grouping ~330 articles, so allow 7.
    http = types.HttpOptions(timeout=420_000)
    client = (genai.Client(http_options=http) if ENTERPRISE  # project/location/creds from env
              else genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=http))
    # Temperature left at the default (1.0): Google advises against lowering it for Gemini 3.
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=list[Story],
    )
    # Main model: medium thinking (Gemini 3 Flash defaults to high, which ran past the
    # deadline). The fallback keeps its own default.
    main_config = config.model_copy(update={
        "thinking_config": types.ThinkingConfig(thinking_level=THINKING)})

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
            print(f"Gemini answered: {m} via {'Agent Platform' if ENTERPRISE else 'Gemini API'} "
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
        angles = {n.id: n.angle.strip() for n in st.notes} if len(ids) > 1 else {}
        stories.append({
            "level": st.level if st.level in LEVELS else "National",
            "topic": st.topic if st.topic in TOPICS else "Other public interest",
            "headline": st.headline.strip(),
            "summary": st.summary.strip(),
            "articles": [dict(items[i], angle=angles.get(i, "")) for i in ids],
        })
    # Level first (international -> GTA); Gemini's significance order within each level.
    stories.sort(key=lambda st: LEVELS.index(st["level"]))
    return stories


def by_newspaper(feeds, stories):
    """[(outlet, [(article, story), ...])] in feed order; one article per story per outlet.
    A merged wire copy is listed under every outlet that ran it (outlet + also_in)."""
    groups = {o: [] for o in feeds}
    for st in stories:
        seen = set()
        for a in st["articles"]:
            for o in [a["outlet"]] + a.get("also_in", []):
                if o not in seen:
                    seen.add(o)
                    groups.setdefault(o, []).append((a, st))
    return [(o, g) for o, g in groups.items() if g]


def by_level(stories):
    """[(level, [story, ...])] in LEVELS order."""
    groups = {lv: [] for lv in LEVELS}
    for st in stories:
        groups[st["level"]].append(st)
    return [(lv, g) for lv, g in groups.items() if g]


def byline(a):
    """'Outlet · Author' (also lists other outlets that ran the same report)."""
    parts = [a["outlet"]] + ([a["author"]] if a.get("author") else [])
    s = " · ".join(parts)
    if a.get("also_in"):
        s += f" (also in {', '.join(a['also_in'])})"
    return s


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
            for o in [a["outlet"]] + a.get("also_in", []):
                picked[o] = picked.get(o, 0) + 1
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
    small = "<span style='font-size:13px;color:#555'>"

    def article_lines(st):
        """HTML + text lines: title link, outlet · author, and (if several) how it differs."""
        hs, ts = [], []
        for a in st["articles"]:
            angle = f"<br><i>{esc(a['angle'])}</i>" if a.get("angle") else ""
            hs.append(f"<li><a href='{esc(a['link'], quote=True)}'>{esc(a['title'])}</a> "
                      f"{small}— {esc(byline(a))}{angle}</span></li>")
            ts += [f"  • {a['title']} — {byline(a)}"] + ([f"    {a['angle']}"] if a.get("angle") else []) + [f"    {a['link']}"]
        return "<ul style='margin-top:4px'>" + "".join(hs) + "</ul>", ts

    if group_by == "level":
        for level, sts in by_level(stories):
            section(level)
            for st in sts:
                arts_h, arts_t = article_lines(st)
                h.append(f"{li}<b>{esc(st['headline'])}</b><br>{esc(st['summary'])}{arts_h}</li>")
                t += [st["headline"], st["summary"]] + arts_t + [""]
            h.append("</ul>")
    elif group_by == "topic":
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
                h.append(f"{li}<a href='{esc(a['link'], quote=True)}'><b>{esc(a['title'])}</b></a> "
                         f"{small}— {esc(byline(a))}</span><br>{esc(st['summary'])}</li>")
                t += [f"{a['title']} — {byline(a)}", st["summary"], a["link"], ""]
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
    """Write digests/<date>.json (format 3: stories with level, authors, angles) and refresh index."""
    DIGEST_DIR.mkdir(exist_ok=True)
    date = now.strftime("%Y-%m-%d")
    digest = {
        "format": 3,
        "date": date,
        "generated_at": now.isoformat(timespec="seconds"),
        "scanned": total_scanned,
        "scanned_by_outlet": {o: counts.get(o, 0) for o in feeds},
        "outlet_order": list(feeds),
        "topic_order": TOPICS,
        "level_order": LEVELS,
        "stories": [
            {"level": st["level"], "topic": st["topic"], "headline": st["headline"],
             "summary": st["summary"],
             "articles": [{"outlet": a["outlet"], "author": a["author"], "title": a["title"],
                           "link": a["link"], "angle": a["angle"], "also_in": a["also_in"]}
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
