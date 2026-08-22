"""CLI entrypoint: python -m factory <command>

  run [provider] [--days N] [--url URL]
                               scrape N days of guide windows (default 1) for one or all
                               providers; --url overrides to a single page (one provider
                               only — demo-drift points it at a tunneled fixture)
  catalog                      refresh the Philo title -> record-URL map
  rank                         compute the top-20 upcoming titles, write them to Port
  publish                      publish the top-20 to Port's Context Lake (webapp backend)
  verify                       run every scraper against its fixture (exit 1 on mismatch)
  heal <provider>              force a heal cycle
  fixtures                     capture fresh guide-window HTML fixtures for all providers
"""
from __future__ import annotations

import json
import sys

from . import brightdata, catalog, pipeline, publish, rank, telemetry
from .agents import healer


def main(argv: list[str]) -> int:
    telemetry.init()
    if not argv:
        print(__doc__)
        return 2

    command, *rest = argv

    if command == "run":
        url, days = None, 1
        if "--url" in rest:
            i = rest.index("--url")
            if i + 1 >= len(rest):
                print("usage: run <provider> [--days N] [--url <URL>]")
                return 2
            url = rest[i + 1]
            rest = rest[:i] + rest[i + 2 :]
        if "--days" in rest:
            i = rest.index("--days")
            if i + 1 >= len(rest) or not rest[i + 1].isdigit():
                print("usage: run <provider> [--days N] [--url <URL>]")
                return 2
            days = int(rest[i + 1])
            rest = rest[:i] + rest[i + 2 :]
        providers = rest or list(pipeline.PROVIDERS)
        if url and len(providers) != 1:
            print("--url requires exactly one provider")
            return 2
        for provider in providers:
            print(json.dumps(pipeline.run_once(provider, days=days, target_url=url), indent=2))
        return 0

    if command == "catalog":
        print(json.dumps(catalog.refresh(), indent=2))
        return 0

    if command == "rank":
        top = rank.compute()
        print(json.dumps(top, indent=2))
        if top:
            rank.write_to_port(top)
        return 0 if top else 1

    if command == "publish":
        print(json.dumps(publish.push(), indent=2))
        return 0

    if command == "verify":
        return pipeline.verify_all()

    if command == "heal":
        if not rest:
            print("usage: heal <provider> [drift description]")
            return 2
        provider = rest[0]
        drift = " ".join(rest[1:]) or "manual heal request"
        ok = healer.heal(provider, pipeline.collector_id(), drift, "manual")
        return 0 if ok else 1

    if command == "fixtures":
        # One real channel page, frozen. Trim it (see CLAUDE.md) before using it for
        # collector creation; hand-check channel.expected.json against it.
        url = "https://streamingtvguides.com/Channel/AETV"
        brightdata.capture_fixture(url, "fixtures/channel.full.html")
        print(f"captured fixtures/channel.full.html from {url}")
        return 0

    print(f"unknown command: {command}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
