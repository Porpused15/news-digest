#!/usr/bin/env python3
"""Test only: fetch the feeds once, run prompt/thinking variants on the same articles.

No email, nothing committed. Writes compare/input.json and compare/results.json (uploaded
as a workflow artifact) and prints story counts per variant.

  A  current prompt, medium thinking (what runs every morning)
  B  reworded prompt (complete digest, no limit on stories), medium thinking
  C  reworded prompt, high thinking
"""
import json
import os
import sys
import time
from pathlib import Path

import news_digest as nd
from google.genai import types

CURRENT = nd.PROMPT
REWORDED = CURRENT.replace(
    "Build a morning news briefing for a reader in Toronto who follows politics and public affairs.",
    "Build a complete morning digest for a reader in Toronto who follows politics and public "
    "affairs. Completeness matters more than brevity: there is no limit on the number of "
    "stories, and every article that meets the criteria below should be in a story, "
    "including minor ones.",
).replace(
    "1. Select articles about",
    "1. Select every article about",
)
assert REWORDED != CURRENT and REWORDED.count("every article") == 2

VARIANTS = [
    ("A", CURRENT, types.ThinkingLevel.MEDIUM),
    ("B", REWORDED, types.ThinkingLevel.MEDIUM),
    ("C", REWORDED, types.ThinkingLevel.HIGH),
]


def main():
    cfg = nd.load_config(os.environ.get("CONFIG_PATH", "config.json"))
    items, errors, counts = nd.fetch_items(cfg["feeds"], int(cfg["lookback_hours"]))
    print(f"Fetched {len(items)} items; {len(errors)} feed errors", file=sys.stderr)
    for i, it in enumerate(items):
        it["_i"] = i  # carried into each story's article copies; not sent to Gemini
    out = Path("compare"); out.mkdir(exist_ok=True)
    (out / "input.json").write_text(json.dumps(
        {"items": items, "errors": errors, "counts": counts}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    results = {}
    only = os.environ.get("VARIANTS", "ABC")
    for name, prompt, thinking in VARIANTS:
        if name not in only:
            continue
        nd.PROMPT, nd.THINKING = prompt, thinking
        t0 = time.monotonic()
        try:
            stories = nd.summarize(items, nd.MAIN_MODEL, cfg["summary_language"])
        except Exception as exc:
            print(f"{name}: failed: {exc}", file=sys.stderr)
            results[name] = {"error": str(exc)}
            continue
        secs = round(time.monotonic() - t0)
        ids = [a["_i"] for st in stories for a in st["articles"]]
        results[name] = {
            "seconds": secs,
            "stories": [{"level": st["level"], "headline": st["headline"],
                                  "ids": [a["_i"] for a in st["articles"]]} for st in stories],
        }
        print(f"{name}: {len(stories)} stories, {len(ids)} articles, {secs} s", file=sys.stderr)

    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                      encoding="utf-8")


if __name__ == "__main__":
    main()
