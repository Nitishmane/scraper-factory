#!/usr/bin/env python3
"""Derive fixtures/channel.mutated.html from fixtures/channel.html.

Same programs, same times, same meaning -- but every structural handle a generated
scraper leans on is moved: classes renamed, cards reordered, time text wrapped in an
extra element. A selector-based scraper breaks; the semantic prompt should heal.

    python3 scripts/mutate_channel_fixture.py
"""
from __future__ import annotations

import re
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

RENAMES = {
    "program-card-main": "entry-body",
    "program-card": "sched-entry",
    "program-time": "airing-when",
    "program-list": "sched-list",
    "program-detail-trigger": "entry-more",
    "section-title": "sched-head",
}


def main() -> int:
    html = (FIXTURES / "channel.html").read_text()

    for old, new in RENAMES.items():
        html = html.replace(old, new)

    # wrap the time strong tags in an extra element
    html = re.sub(r"<strong>(\d{1,2}:\d{2} [AP]M)</strong>", r"<strong><em>\1</em></strong>", html)

    # reorder the schedule cards (reverse), meaning unchanged
    cards = re.findall(r'<article class="sched-entry">.*?</article>', html, re.S)
    if cards:
        block_start = html.find(cards[0])
        block_end = html.find(cards[-1]) + len(cards[-1])
        html = html[:block_start] + "".join(reversed(cards)) + html[block_end:]

    (FIXTURES / "channel.mutated.html").write_text(html)
    print(f"channel.mutated.html: {len(html)} bytes, {len(cards)} cards reordered, "
          f"{len(RENAMES)} classes renamed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
