# Scraper Factory — agent rules

Read this before touching the repo. It is the contract for how scrapers are built, repaired,
and promoted here.

## What this scrapes

**TV guide data via per-channel pages** (streamingtvguides.com/Channel/<ID> — a flat list
of ~455 program cards spanning ~12 days, one page per channel), plus the **Philo catalog**
(philo.com/go/allshows) for title → record-URL mapping. Provider → channel lineups are
parsed deterministically from each provider's guide page anchors with a plain fetch (no AI
scraper). The ranker joins guide + catalog + TMDB and serves the top-20 at `/top20`.

Times on the site display in **America/New_York**; UTC conversion happens locally in
`pipeline._parse_card_times`, never in the scraper.

## Bright Data Scraper Studio

Command name differs between docs (`bdata`) and the GitHub README (`brightdata`). Confirm with
`brightdata --help` and set `BRIGHTDATA_BIN` accordingly.

```
SCRAPER_STUDIO_COLLECTOR_ID_CHANNEL=c_REPLACE_ME        # any streamingtvguides.com/Channel/<ID> page (one shared collector)
SCRAPER_STUDIO_COLLECTOR_ID_PHILO_CATALOG=c_mt4ljtldpkz0bb9gi  # catalog: philo.com/go/allshows
```

Pin the real IDs above after `brightdata scraper create` so scrapers are reused across
sessions instead of recreated.

**Creating the channel collector is a known dance** (AI generation fails on full-size
pages AND bakes in a listing→detail crawl if it sees `/watch/` links):
1. Freeze a real channel page (`python -m factory fixtures`), trim to ~30
   `program-card` articles, **strip every `data-program-watch-href` and `/watch/` link**
   (`scripts/mutate_channel_fixture.py` documents the class names involved).
2. Serve `fixtures/` via tunnel, `brightdata scraper create <tunnel>/channel.html
   "<PROMPT from src/factory/pipeline.py>"`.
3. Run it against the tunneled fixture, hand-check the rows, freeze
   `fixtures/channel.expected.json`, then run against a live /Channel page to confirm
   it walks all ~455 cards.

**Collectors run in Bright Data's cloud and cannot reach localhost.** Anything a collector
must scrape — including the frozen fixtures used by `make verify` and `make demo-drift` —
has to be served through a public tunnel (`make serve-fixtures` + `cloudflared tunnel --url
http://localhost:9000`, then set `FIXTURE_BASE_URL`). Never "fix" the drift demo by swapping
local fixture files; the collector never sees them.

**Collectors also cannot reach streaming-media domains — Bright Data policy, confirmed by
their support (2026-08-22), who endorsed our workaround.** Production scraping is therefore
the fetch/extract split in `brightdata.run_with_relay()`: Web Unlocker fetches the live
page, the collector extracts from the tunneled copy in `fixtures/relay/` (30-min cache via
`RELAY_CACHE_MINUTES`; direct is retried first in every new process). Do not "fix" the
resulting `proxy_config` 403s by changing zones — support says it is policy.

**Scrape volume:** a full `make run` is ~144 page fetches (48 windows × 3 providers). Use
`python -m factory run philo --days 1` during development and watch `brightdata budget`.

## Scraper prompts — semantic, never structural

Prompts describe what the data **means**, not where it sits in the DOM.

- Correct: "the start and end time of each program, exactly as displayed"
- Wrong: "the third span inside each grid cell"

The prompt text is the healing artifact — it is what `scraper heal` reasons over, and it is
what survives a redesign. Canonical prompts: `src/factory/pipeline.py::PROMPT` (guide) and
`src/factory/catalog.py::PROMPT` (catalog). If you change one, update the `prompt_text`
property on the Port `scraper` entity too, so repairs stay auditable.

**`brightdata scraper create` caps the description at 500 characters** and rejects longer
ones with `Invalid description` (HTTP 400). Both canonical prompts fit; keep them under the
cap. AI generation takes 5–10+ minutes per collector and the platform caps concurrent
builds (the CLI retries 429s automatically) — run creates in the background, never with a
short foreground timeout.

Always capture the raw string beside the parsed value: `start_raw: "8:00 PM"` next to
`start_utc: "2026-08-24T00:00:00Z"`. A `start_raw` that parses to a null `start_utc` is a
drift signal.

## Heal workflow — do not deviate

```bash
brightdata scraper heal <collector_id> "<plain-language description of what broke>"
# -> envelope: status="awaiting_approval", preview_result=[...]
```

1. Run `heal` with a description of the observed symptom, not a guess at the fix.
2. Diff `preview_result` against `fixtures/<provider>.expected.json`.
3. Pass → `brightdata scraper approve <collector_id>`
4. Fail → `brightdata scraper approve <collector_id> --reject`, set the Port scraper's
   `health` to `broken`, and leave it for a human.

**Never approve without a passing fixture diff.** The fixture is the only promotion gate in
this system. Judgment may be delegated to an agent; verification may not.

## Architecture invariants

- The **Verifier is deterministic code**, never an LLM. A gate you cannot trust
  deterministically is not a gate.
- **Two trigger paths, one idempotent `/heal` endpoint.** The inline detector sets Port
  `health = drifting` within seconds; the SigNoz alert webhook arrives up to ~5 minutes later
  because notifications are grouped. Either may fire first; neither may double-promote.
- **Drift vs. real change.** A broken scraper and a genuinely changed guide look identical in
  one sample. Lineup churn needs two consecutive confirming runs, and the fixture arbitrates:
  if the candidate still reproduces `expected.json` from frozen HTML, the scraper is fine and
  the guide really changed.
- **Fixtures are frozen inputs.** Never regenerate `*.expected.json` to make a test pass.
  (`scripts/gen_placeholder_fixtures.py` exists only to bootstrap placeholder fixtures before
  real captures; it must never run as part of making verification pass.)
- **Drifted runs never feed the ranker.** `run_once` only persists guide rows when the drift
  detector is clean.
- The catalog scraper has **no fixture** (the catalog legitimately changes daily); it is
  gated by `catalog.MIN_ROWS` instead.

## Conventions

- Scrape work is traced under `scrape.*` spans (one `scrape.fetch` per guide window);
  ranking under `rank.compute`, catalog under `catalog.refresh`. The service runs under
  `opentelemetry-instrument` so FastAPI endpoints are traced too.
- Port writes always use `upsert=true&merge=true`. Health writes never raise — a Port outage
  must not kill a scrape or repair.
- State lives in SQLite at `out/factory.db` (snapshots, guide rows, catalog map, TMDB cache).
  It is disposable; delete it to reset.
- Never write secrets into this file. Credentials belong in `.env`.
