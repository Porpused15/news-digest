#!/usr/bin/env python3
"""Hourly gate for the scheduled workflow (stdlib only, runs before installing deps).

Runs the digest at the first scheduled run at or after config.json's send_hour
(Toronto time), once per day. Handles daylight-saving changes and late or skipped
GitHub cron runs. Prints run=true/false to $GITHUB_OUTPUT.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

now = datetime.now(ZoneInfo("America/Toronto"))
try:
    send_hour = int(json.loads(Path("config.json").read_text(encoding="utf-8")).get("send_hour", 6))
except (TypeError, ValueError):
    send_hour = 6
if not 0 <= send_hour <= 23:
    send_hour = 6
already_done = Path(f"digests/{now:%Y-%m-%d}.json").exists()
run = now.hour >= send_hour and not already_done

print(f"Toronto {now:%Y-%m-%d %H:%M}, send_hour={send_hour}, done_today={already_done} -> run={run}")
out = os.environ.get("GITHUB_OUTPUT")
if out:
    with open(out, "a") as f:
        f.write(f"run={'true' if run else 'false'}\n")
