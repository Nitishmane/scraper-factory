# The Scraper Factory

**What should I record this week?** This project scrapes a week of TV guide data for
Philo, YouTube TV, and Sling, ranks the top 20 upcoming movies and shows by TMDB
popularity, and gives you a one-click path to each title's Philo page — where **Record**
is one more click.

Underneath the product is the actual thesis: web scrapers rot. A guide site ships a
redesign, selectors stop matching, and the data either stops arriving or — worse —
arrives subtly wrong. This treats that as a reliability problem with an on-call rotation.
Scrapers are catalogued services with health states. Every run is traced. Drift is an
alert. Repair is an automated loop that must prove itself against a frozen fixture before
its output is trusted, and escalates to a human when it can't.

Built on **Port** (factory + catalog + governance), **Bright Data Scraper Studio** (the
scrapers, via CLI), and **SigNoz** (traces, metrics, logs).

---

## Quickstart

**Prerequisites:** Python 3.11+, Node 20+, Docker, a Bright Data API key, a Port account,
a SigNoz instance, and (optional but recommended) a free TMDB API key.

```bash
git clone <this-repo> && cd scraper-factory
cp .env.example .env          # fill in credentials

# SigNoz (self-hosted, recommended — no key, no trial clock)
git clone -b main https://github.com/SigNoz/signoz.git ../signoz
docker compose -f ../signoz/deploy/docker/docker-compose.yaml up -d
# UI at http://localhost:8080, OTLP at http://localhost:4318

make setup                    # deps + OTel auto-instrumentation
make bootstrap                # Port: blueprints, automation, action, scorecard, dashboard (idempotent)
python3 scripts/bootstrap_signoz.py   # SigNoz: dashboard + drift alert + webhook channel (idempotent)
make serve                    # factory API on :8000, auto-instrumented

make run                      # scrape 7 days of channel pages, all providers (GUIDE_MAX_CHANNELS each)
make catalog                  # map titles -> Philo record URLs
make rank                     # compute the top 20; open http://localhost:8000/top20
make publish                  # push the top 20 to Postgres for the webapp
make demo-drift               # manufacture drift and watch the repair loop
make verify                   # fixture gate; non-zero exit on mismatch
```

An architecture walkthrough for judges lives at `demo/index.html` (self-contained, works
offline): platforms, what-calls-what, the fetch/extract split, the Port control plane,
agents, and schemas.

Port's automation needs to reach `/heal`, so expose it with a tunnel
(`cloudflared tunnel --url http://localhost:8000`) and set `HEAL_WEBHOOK_URL` before
`make bootstrap`.

Bright Data collectors run in Bright Data's cloud and **cannot reach localhost**, so
`make verify` and `make demo-drift` scrape the frozen fixtures through a second tunnel:

```bash
make serve-fixtures                             # fixtures/ on :9000
cloudflared tunnel --url http://localhost:9000  # separate terminal; prints an https URL
# set FIXTURE_BASE_URL in .env to that URL
```

### First-run setup

Two collectors serve everything: **one shared channel-schedule scraper** (every
streamingtvguides channel page has the same structure; providers differ only in lineup)
and the Philo catalog scraper.

```bash
brightdata login --api-key $BRIGHTDATA_API_KEY
# catalog scraper: created directly against the live page
brightdata scraper create "https://www.philo.com/go/allshows" "<PROMPT from src/factory/catalog.py>"
```

Creating the **channel collector** requires a known dance (Bright Data's AI generation
fails on full-size pages and bakes in a bad crawl if the sample has followable links) —
create it against the trimmed fixture served through the tunnel:

```bash
brightdata scraper create "$FIXTURE_BASE_URL/channel.html" "<PROMPT from src/factory/pipeline.py>"
# then: run it on the fixture, hand-check the rows, freeze fixtures/channel.expected.json
# pin both collector_ids in .env AND CLAUDE.md (full steps: CLAUDE.md "known dance")
```

---

## The data

Every channel on streamingtvguides.com has its own page (`/Channel/<ID>`) — a flat list
of **~455 program cards spanning ~12 days**. One shared collector extracts any of them;
the provider → channel mapping is parsed deterministically from each provider's guide
page anchors (Philo 137 channels, YouTube TV 175, Sling 119). A run scrapes
`GUIDE_MAX_CHANNELS` pages per provider (default 25); times display in Eastern and are
converted to UTC locally, never by the scraper. Watch spend with `brightdata budget`.

The Philo record links come from a second scrape: `philo.com/go/allshows` links every
title to its player page (`philo.com/player/show/<id>`); those IDs are internal blobs —
scraping is the only way to get them. philo.com is a JS-rendered SPA, so when the
collector sees an empty static copy, `catalog._parse_embedded()` falls back to the show
IDs embedded as JSON in the fetched page (358 titles mapped). The ranker joins guide and
catalog on normalized title.

**The fetch/extract split.** Bright Data's policy blocks streaming-media domains for
collector runs (confirmed by their support, who endorsed this pattern), so scraping is a
two-product pipeline: the **Web Unlocker** (`brightdata scrape`) fetches the live page,
and the **Scraper Studio collector** extracts from a tunneled copy served out of
`fixtures/relay/`. The code always tries a direct collector run first, so if policy ever
changes the relay disappears by itself — and every scrape's input HTML is archived, which
makes drift investigations replayable byte-for-byte.

Ranking = 0.6·TMDB popularity + 0.25·TMDB rating + 0.1·airing volume, with boosts for
NEW/premiere badges and prime-time slots. TMDB responses are cached in SQLite, so
re-ranking is free and offline.

---

## The webapp — a Context Lake consumer (Vercel-ready)

`webapp/` is a Next.js app showing the top 20 as clickable cards — each one links to the
title's Philo page, where **Record** is one click for a logged-in subscriber. It has **no
database of its own**: it reads the `ranked_title` entities straight from Port's Context
Lake, the same catalog the operator dashboard and the Triage agent use. `make publish`
(= rank + upsert to Port) is the seam; the page revalidates every 60 seconds.

```bash
# local dev — put PORT_CLIENT_ID / PORT_CLIENT_SECRET / PORT_API_BASE in webapp/.env.local
cd webapp && npm install && npm run dev      # http://localhost:3000
```

**Deploy to Vercel:**

1. Push this repo to GitHub and import it in Vercel, setting **Root Directory** to
   `webapp/`.
2. Set three environment variables on the project: `PORT_CLIENT_ID`,
   `PORT_CLIENT_SECRET`, `PORT_API_BASE` (`https://api.getport.io/v1` for EU orgs).
3. Deploy. `make publish` from your laptop updates the live site within a minute.

Without reachable Port credentials the app renders sample data behind a visible banner,
so the demo never white-screens.

---

## Architecture

```
Port brief (self-service action, human approval)
        │
        ▼
   Triage agent (Port-native) ──► Builder ──► Verifier ──► Promoter ──► live scraper
                  ▲                              ▲                          │
                  │                              │                          ▼
                  │                           Healer                  traces + metrics
                  │                              ▲                     + logs → SigNoz
                  │              ┌───────────────┴──────────────┐            │
                  │              │                              │            │
                  └──── inline detector (seconds)      SigNoz alert ◄─────────┘
                         via Port automation           (safety net, ~5 min)

   Verifier fails ──► approve --reject ──► health = broken ──► human queue in Port

   guide rows ──► SQLite ──► ranker (TMDB) ──► /top20 + Port ranked_title entities
```

| agent | implementation | role |
|---|---|---|
| **Triage** | Port-native agent | Judgment: read run history, decide heal vs escalate, author the drift description |
| **Builder** | `agents/builder` | `brightdata scraper create` |
| **Verifier** | `agents/verifier.py`, **deterministic** | Diff `preview_result` against `expected.json` |
| **Healer** | `agents/healer.py` | `brightdata scraper heal` |
| **Promoter** | `agents/healer.py` | `scraper approve` / `--reject`, Context Lake writes |

**Judgment goes to the agent. Proof stays deterministic.** The Triage agent decides what
broke and whether repair is worth attempting. The Verifier is plain code, because a
verification gate you can't trust deterministically isn't a gate.

---

## The five Port pillars

| pillar | use |
|---|---|
| **Context Lake** | Factory state: `data_source`, `scraper`, `scrape_run`, `heal_event`, `ranked_title` |
| **Workflows & Tools** | Self-service action (takes a brief) + the drift automation |
| **AI Agents** | Port-native Triage agent; coding agent as platform consumer via MCP |
| **Governance Layer** | Approval on the action, scorecard on scrapers, scoped agent tokens |
| **Interface Builder** | Factory Operator dashboard: SigNoz embedded + the clickable top-20 table |

Port shows **fleet state**; SigNoz shows **telemetry**. Catalog versus time series — each
answers a question the other can't.

---

## Why two trigger paths

SigNoz evaluates alert rules every minute but **groups webhook deliveries every ~5 minutes**.
Waiting on `drift → alert → webhook → heal` would take longer than a demo runs, so there are
two independent detectors feeding one idempotent `/heal` endpoint:

| path | trigger | latency | role |
|---|---|---|---|
| **Fast** | inline detector sets Port `health = drifting` → automation fires | seconds | what production wants |
| **Safety net** | SigNoz metrics alert → webhook | ~5 min | catches what the inline detector can't — including the pipeline dying entirely, via "alert when data stops coming" |

The inline detector is the reflex; SigNoz is the immune system.

---

## Broken scraper vs. real change

A drifted scraper and a genuinely changed guide look identical in the data. Resolved two
ways:

1. **Two consecutive confirming runs** before a change (e.g. lineup churn — channels come
   and go legitimately) is accepted as real (`state.confirm_change`).
2. **The fixture arbitrates.** If a candidate still reproduces `expected.json` from frozen
   HTML, the scraper is healthy and the guide really changed. If it can't, the scraper
   drifted.

Drift rules for guide data: rows-per-page out of band · required-field null rate ·
`start_raw` present but `start_utc` parsed to null · **channel collapse** (< 80% of the
requested channels yielded rows) · empty-page rate · schema hash change · **lineup
disappearance** (previously-seen channels vanishing; growth is normal — a widened scope
or new carriage). Drift state is keyed per `collector:provider`, so providers sharing the
collector never compare against each other's history.

---

## Verification

```
fixtures/
  channel.html            # a REAL captured channel page, trimmed to 30 program cards
  channel.expected.json   # hand-checked scraper output for that exact page
  channel.mutated.html    # same programs and times, every structural handle moved
  relay/                  # archived inputs of every relayed scrape (gitignored)
```

`make verify` scrapes the **tunneled frozen fixture** (`FIXTURE_BASE_URL` — Bright Data's
cloud collectors can't reach localhost, hence the tunnel) and diffs the output against
`channel.expected.json`, exiting non-zero on mismatch — the CI gate in one line, same
input every run. Without `FIXTURE_BASE_URL` it falls back to a live channel page and logs
a warning, because a gate against a moving target isn't reproducible. Nothing is ever
promoted without a passing diff.

`channel.mutated.html` (generated by `scripts/mutate_channel_fixture.py`) renames the
card classes, reorders the cards, and wraps airtimes in extra elements while keeping
every program and time identical. A selector-based scraper breaks on it; a semantic
prompt should heal. `make demo-drift` points the collector at the tunneled mutated page
to inject that drift on demand.

---

## Observability

Three signals, on both the pipeline and the API endpoints.

**Traces:** `scrape.run` → one `scrape.fetch` per channel page (carrying
`target.provider`, `channel.id`, `channel.rows`, `fetch.mode`, and `relay.cache_hit`) →
`scrape.extract` / `scrape.validate` / `scrape.heal.reprompt` / `scrape.verify.fixture`,
plus `catalog.refresh`, `rank.compute` (with `rank.titles_considered` /
`rank.titles_matched`), `tmdb.lookup`, and `port.upsert`. Running under
`opentelemetry-instrument` also traces FastAPI, so **Port's automation calling `/heal`
appears as a span** — the integration is in the trace, not just this README.

**Metrics:** `scraper.rows_returned`, `scraper.field.null_rate`, `scraper.drift.detected`,
`scraper.heal.attempts`, `scraper.heal.success`, `scraper.verify.pass`,
`scraper.run.duration`, `ranker.titles_considered`, `ranker.catalog_match_rate`, plus the
per-operation signals: `scrape.fetch.duration` (histogram, by provider + `fetch.mode`),
`scrape.rows.extracted` and `scrape.pages.fetched` (by provider), `relay.cache`
(`hit=true/false`), `heal.events` (`outcome=repaired/escalated/rejected`),
`port.write.duration` (histogram, by `blueprint` + `success`), and `tmdb.cache`
(`hit=true/false`).

**Logs:** `telemetry.init()` attaches an OTLP `LoggingHandler` to the root logger, so
ordinary `logging.getLogger(__name__)` calls flow to SigNoz with `trace_id`/`span_id`
injected whenever a span is active. This works identically for the uvicorn service and
for `python -m factory …` CLI runs; an `atexit` `force_flush` guarantees a short-lived CLI
process ships its buffered logs before exiting. The service runs under
`opentelemetry-instrument` with `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=false`
so the wrapper does not double-export what the in-process handler already sends. The drift
description, the heal prompt, and the verifier's diff are all logged — the audit trail for
every automated repair. Click a `heal_event`'s trace link in Port, land on the trace,
pivot to the logs for that exact repair.

**Alert:** `scraper.drift.detected` above 0, "at least once" over a 5-minute rolling
window, checked every minute. Enable "alert when data stops coming" for the dead-pipeline
case.

---

## Limitations — what's real and what isn't

- **Bright Data policy blocks collector runs against streaming-media domains** (their
  support confirmed and endorsed the fetch/extract split). Consequences: runs in the
  Bright Data console show relay-tunnel URLs (the *content* is live — the Web Unlocker
  fetched it seconds earlier), and the factory depends on the fixtures tunnel being up.
- **Quick-tunnel URLs rotate on every restart** (`trycloudflare.com`). After restarting a
  tunnel, update `FIXTURE_BASE_URL` / `HEAL_WEBHOOK_URL` in `.env` and re-run
  `make bootstrap` so Port's automation points at the new URL.
- **The catalog scraper has no fixture gate** — the Philo catalog is a moving target by
  design, so it's gated by a row-count band instead (`catalog.MIN_ROWS`). And because
  philo.com is a JS SPA, the collector path yields nothing from static copies — the
  embedded-JSON parser is what delivers in practice.
- **Catalog coverage is partial**: `go/allshows` renders ~358 titles of Philo's much
  larger library, so roughly half the top-20 falls back to guide links instead of direct
  record pages.
- **TMDB is optional**: without a key the ranking degrades to guide heuristics (airing
  volume, prime time, NEW badges) and is noticeably worse.
- **streamingtvguides.com is a third-party aggregator** — guide accuracy (and the
  Eastern-time display the local UTC conversion assumes) is theirs.
- **The Triage agent is registered in Port but thin.** It authors drift descriptions; it
  does not yet reason over long run histories.
- **One Port UI step isn't scripted**: embedding the SigNoz dashboard as an iframe widget
  on Factory Operator. Everything else — blueprints, automation, action, scorecard,
  operator dashboard, the Triage AI agent, SigNoz dashboard/alert/channel — is provisioned
  by `bootstrap_port.py` and `bootstrap_signoz.py`.
- **No retry/backoff on Bright Data calls.** A transient CLI failure surfaces as an error
  (mitigated for fetches by the 30-minute relay cache, `RELAY_CACHE_MINUTES`).
