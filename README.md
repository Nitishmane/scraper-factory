# The Scraper Factory

**What should I record this week?** This project scrapes ~12 days of TV guide data per channel
for Philo, YouTube TV, and Sling, ranks the **top 10 upcoming shows and top 10 upcoming movies**
by TMDB popularity, and gives you a one-click path to each title's Philo page — where **Record**
is one more click.

Underneath the product is the actual thesis: web scrapers rot. A guide site ships a
redesign, selectors stop matching, and the data either stops arriving or — worse —
arrives subtly wrong. This treats that as a reliability problem with an on-call rotation.
Scrapers are catalogued services with health states. Every run is traced. Drift is an
alert. Repair is an automated loop that must prove itself against a frozen fixture before
its output is trusted, and escalates to a human when it can't.

The same doctrine now extends past the scrapers to the code itself: an operator files a
feature request in Port, a **locally-run** Claude Code builder opens a PR, deterministic CI
gates it, a human merges, and a deploy workflow ships it and records the deployment back in
Port. **Judgment goes to the agent. Proof stays deterministic.**

Built on **Port** (control plane: catalog, Context Lake, actions, automations, scorecards,
dashboards, AI agents), **Bright Data Scraper Studio** (the scrapers, plus self-healing),
**SigNoz** (traces, metrics, logs), and **TMDB** (the ranking signal).

**Live webapp:** <https://scraper-factory-top20.vercel.app> · **Demo walkthrough:** open
[`demo/index.html`](demo/index.html) (a self-contained multipage site: story, architecture
diagrams, the agent workforce, and the platform).

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
make bootstrap                # Port: blueprints, automations, actions, scorecard, dashboard, agents (idempotent)
python3 scripts/bootstrap_signoz.py   # SigNoz: dashboard + drift alert + webhook channel (idempotent)
make serve                    # factory API on :8000, auto-instrumented

make run                      # scrape ~7 days of channel pages, all providers (GUIDE_MAX_CHANNELS each)
make catalog                  # map titles -> Philo record URLs (shows + movies)
make rank                     # compute the top 10 shows + top 10 movies
make publish                  # push the ranking to Port's Context Lake (the webapp reads it there)
make demo-drift               # manufacture drift and watch the repair loop
make verify                   # fixture gate; non-zero exit on mismatch
```

Port's automation needs to reach `/heal` (and the self-service actions reach `/build` and
`/feature`), so expose the factory with a tunnel
(`cloudflared tunnel --url http://localhost:8000`) and set `HEAL_WEBHOOK_URL`,
`BUILD_WEBHOOK_URL`, and `FEATURE_WEBHOOK_URL` before `make bootstrap`.

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

The Philo record links come from a second scrape. `philo.com/go/allshows` links every
show to its player page (`philo.com/player/show/<id>`), and `philo.com/go/allmovies` does
the same for movies — an additive merge into one catalog (~739 titles total). Those IDs
are internal base64 blobs, so scraping is the only way to get them. philo.com is a
JS-rendered SPA, so when the collector sees an empty static copy, `catalog._parse_embedded()`
falls back to the show IDs embedded as JSON in the fetched page. The ranker joins guide and
catalog on normalized title.

**The fetch/extract split.** Bright Data's policy blocks streaming-media domains for
collector runs (confirmed by their support, who endorsed this pattern), so scraping is a
two-product pipeline: the **Web Unlocker** (`brightdata scrape`) fetches the live page,
and the **Scraper Studio collector** extracts from a tunneled copy served out of
`fixtures/relay/`. The code always tries a direct collector run first, so if policy ever
changes the relay disappears by itself — and every scrape's input HTML is archived, which
makes drift investigations replayable byte-for-byte.

Ranking = 0.6·TMDB popularity + 0.25·TMDB rating + 0.1·airing volume, with boosts for
NEW/premiere badges and prime-time slots, computed **within each kind** so the output is a
top-10 of shows and a top-10 of movies rather than one blended top-20. TMDB responses are
cached in SQLite, so re-ranking is free and offline.

---

## The webapp — a Context Lake consumer (deployed on Vercel)

`webapp/` is a Next.js app showing the ranking as two side-by-side columns — **Shows on the
left, Movies on the right** — each card linking to the title's Philo page, where **Record**
is one click for a logged-in subscriber. It has **no database of its own**: it reads the
`ranked_title` entities straight from Port's Context Lake, the same catalog the operator
dashboard and the AI agents use. `make publish` (= rank + upsert to Port) is the seam; the
route renders per request so it always reflects the live lake.

It is live at **<https://scraper-factory-top20.vercel.app>**.

```bash
# local dev — put PORT_CLIENT_ID / PORT_CLIENT_SECRET / PORT_API_BASE in webapp/.env.local
cd webapp && npm install && npm run dev      # http://localhost:3000
```

**Deploy to Vercel** (manual import):

1. Push this repo to GitHub and import it in Vercel, setting **Root Directory** to
   `webapp/`.
2. Set three environment variables on the project: `PORT_CLIENT_ID`,
   `PORT_CLIENT_SECRET`, `PORT_API_BASE` (`https://api.getport.io/v1`).
3. Deploy. `make publish` from your laptop updates the live site within a minute.

CI/CD also deploys automatically on merge — see [CI, deploy, and the builder](#ci-deploy-and-the-builder).
Without reachable Port credentials the app renders sample data behind a visible banner,
so the demo never white-screens.

---

## The agent workforce

Past the scrapers, the factory is staffed by a mix of agents, deterministic code, and
humans — each with one narrow job. The full roster, with runtimes and human gates, is on the
[workforce page of the demo](demo/workforce.html); the short version:

| Worker | Runtime | Job |
|---|---|---|
| **Intake Clerk** | Port self-service action | `submit_feature_request` creates a `requirement`, posts to `/feature` |
| **Planner + Builder** | **local** Claude Code (headless) | edits a scratch clone (file tools only) |
| **Test Gate** | GitHub Actions `ci.yml` | pytest + `tsc` + `next build` — never an LLM |
| **Reviewer / Merger** | human | reviews the PR and merges — the promotion gate |
| **Deployer** | GitHub Actions `deploy.yml` | Vercel deploy; upserts `factory_deployment`; flips `requirement` |
| **Runner** | Port workflow → `POST /run` (or dev `make run`) | scrapes guide + catalog, writes SQLite + ranked_title |
| **Healer** | Bright Data `scraper heal` | proposes a fix + preview on drift |
| **Verifier** | `agents/verifier.py`, **deterministic** | diffs the preview vs the frozen fixture; the only promoter |
| **Triage / Heal Explainer / On-Call / Catalog Coverage** | Port AI agents | judgment + narration (see below) |
| **Fixture Warden** | human | freezes the `expected.json` answer sheet |

### The requirement → deploy slice

An operator's feature request (`submit_feature_request`) becomes a `requirement` entity and
POSTs to the FastAPI `/feature` endpoint. The **Builder runs locally**, in a scratch clone,
with file tools only; deterministic code in `agents/builder.py` opens branch `req/req_<id>`
and a PR whose body's first line is `Requirement: req_<id>`. GitHub Actions `ci.yml` runs the
deterministic gate (pytest + `tsc --noEmit` + `next build`), a human merges, and `deploy.yml`
deploys to Vercel and upserts a `factory_deployment` entity + flips the `requirement` to
`deployed` (parsing that `Requirement:` line out of the merged PR body).

**Why the builder runs locally:** a deliberate operator decision — **no Anthropic credentials
leave the machine**. GitHub Actions only ever runs no-AI steps (the CI gate and the deploy).
The builder edits files; branch/commit/push/PR mechanics stay in deterministic code, which
also guarantees the PR-body contract the deploy workflow depends on. The builder is
forbidden from touching `fixtures/`, `src/factory/agents/verifier.py`, and the drift
thresholds in `src/factory/detect.py` — the same invariants CLAUDE.md pins — and the
background task aborts if the resulting diff touches any of them.

> A cloud-runner variant of the builder was built and then **deliberately removed** — running
> it would have shipped an Anthropic API key to GitHub Actions, which the operator ruled out.
> The local builder is the only builder; the only workflows in `.github/workflows/` are
> `ci.yml` and `deploy.yml`, both no-AI.

### The Port AI agents

Four agents are registered as `_ai_agent` entities by `make bootstrap`:

| Agent | Mode | Role |
|---|---|---|
| **Triage** | Approval Required | reads run + heal history, decides heal vs escalate, authors the drift description |
| **Heal Explainer** | Automatic (read-only) | narrates what drifted and why the Verifier approved/rejected |
| **On-Call Assistant** | Approval Required | operator Q&A over factory health; can file a `submit_feature_request` |
| **Catalog Coverage** | Automatic (read-only) | reports record-link coverage of the ranked titles |

(The Port workspace also carries stock demo agents from the Port quickstart; only these four
are wired into this system.)

---

## CI, deploy, and the builder

Two GitHub workflows live in `.github/workflows/`, and **neither runs an LLM** — that is the
point. The only Claude in the loop is the local builder on the operator's laptop.

- **`ci.yml`** — on every PR to `main`: a Python gate (`compileall` + `pytest tests/`) and a
  webapp gate (`npm ci` + `tsc --noEmit` + `next build`). **No LLM runs here** — this is the
  deterministic promotion gate.
- **`deploy.yml`** — on PR merge to `main` (or manual dispatch): `vercel pull` / `vercel build`
  / `vercel deploy --prebuilt --prod`, then records a deployment entity in Port and, if the PR
  body carries a `Requirement: req_<id>` line, links the deployment to it and flips the
  requirement to `deployed`.

A cloud-runner builder was prototyped and then removed, so no Anthropic credentials ever
reach GitHub Actions.

**GitHub secrets required** (names only — set them in the repo settings):
`PORT_CLIENT_ID`, `PORT_CLIENT_SECRET`, `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`.
No `ANTHROPIC_API_KEY` lives here — the builder authenticates locally.

> **Vercel git-metadata gotcha.** On a Vercel free/hobby team, a deploy triggered from CI can
> hit a `TEAM_ACCESS_REQUIRED` seat block if git metadata is attached. The fix is to strip git
> metadata from the CI deploy (in progress in `deploy.yml`); the manual-import path above is
> unaffected.
>
> **Blueprint identifier.** `deploy.yml` writes the deployment under the blueprint id in its
> `BLUEPRINT_DEPLOYMENT` env var; `bootstrap_port.py` registers ours as `factory_deployment`
> (a stock `deployment` blueprint already exists in the org). If they disagree, change that one
> env line to match.

---

## Architecture

```
Port action (submit_feature_request)          Port action (request_data_source)
        │                                              │
        ▼                                              ▼
   POST /feature ──► local Claude builder ──► PR    POST /build ──► scraper create ──► Verifier
        │                     │                                                          │
        ▼                     ▼                                                          ▼
   requirement          ci.yml (pytest+tsc+build) ──► human merge ──► deploy.yml ──► Vercel + Port

   drift ──► Port health=drifting ──► automation ──► POST /heal ──► scraper heal ──► Verifier ──► approve/--reject
                   ▲                                     ▲                              (deterministic gate)
                   │                                     │
           inline detector (seconds)          SigNoz alert (~5 min safety net)

   Verifier fails ──► --reject ──► health = broken ──► human queue in Port

   guide rows ──► SQLite ──► ranker (TMDB) ──► /top20 + Port ranked_title ──► Vercel webapp
```

The big, readable versions of these flows — the data pipeline, the fetch/extract relay split,
the self-heal loop, and the requirement→deploy slice — are on the
[architecture page of the demo](demo/architecture.html).

**Judgment goes to the agent. Proof stays deterministic.** The Triage agent decides what
broke and whether repair is worth attempting; the Builder writes code. The Verifier and CI
are plain code, because a verification gate you can't trust deterministically isn't a gate.

---

## Port — the control plane

| pillar | use |
|---|---|
| **Catalog / Context Lake** | Seven blueprints: `data_source`, `scraper`, `scrape_run`, `heal_event`, `ranked_title`, `requirement`, `factory_deployment`; plus two `service` entities. The 20 `ranked_title` entities (`show_01..10`, `movie_01..10`) are the product. |
| **Actions** | `request_data_source` (brief → verified scraper), `submit_feature_request` (requirement → PR), and the operate-now buttons `run_scrape_now` / `refresh_catalog_now` / `publish_now` |
| **Automations & Workflows** | `invoke_healer_on_drift` (`health == "drifting"` → `/heal`); scheduled `wf_scheduled_scrape` / `wf_scheduled_catalog`; event `wf_publish_on_success` / `wf_heal_on_bad_run` — Port operates the factory (see below) |
| **Scorecards** | `scraper_reliability`: Basic → Bronze (verified today) → Silver (verify passing) → Gold (healthy + verified) |
| **Dashboards** | Factory Operator: scrapers-by-health pie, heal-event table, ranked-titles table, architecture note, embedded SigNoz |
| **AI Agents** | Triage, Heal Explainer, On-Call Assistant, Catalog Coverage (the four above) |

All of it is provisioned by `scripts/bootstrap_port.py` — idempotent, re-runnable, no UI
clicks, validated against the live `api.getport.io`. Port shows **fleet state**; SigNoz shows
**telemetry** — catalog versus time series, each answering a question the other can't. The
full pillar-by-pillar breakdown (with SigNoz signals, the Bright Data collectors, and the
schema grid) is on the [platform page of the demo](demo/platform.html).

---

## How Port operates the factory

Port doesn't just catalogue the factory — it **runs** it. The pipeline is moving off manual
`make run` invocation and onto Port-driven orchestration: scheduled workflows, event
workflows, and self-service buttons all POST to the factory's FastAPI service, which executes
the work in the background and returns `202 Accepted` immediately.

**Scheduled workflows (cron, UTC):**

| Workflow | Schedule | Calls |
|---|---|---|
| `wf_scheduled_catalog` | daily 07:30 UTC | `POST /catalog` — refresh the title → record-URL map |
| `wf_scheduled_scrape` | daily 08:00 UTC | `POST /run` — scrape the guide for every provider |

**Event workflows (react to `scrape_run` entities):**

| Workflow | Trigger | Calls |
|---|---|---|
| `wf_publish_on_success` | a `scrape_run` lands `status = success` | `POST /publish` — push the fresh ranking to the Context Lake |
| `wf_heal_on_bad_run` | a `scrape_run` lands `status = error` or `drift` | `POST /heal` — the Triage agent authors the drift description |

**Self-service buttons** (for an operator who wants to force a cycle now):
`run_scrape_now`, `refresh_catalog_now`, `publish_now`.

**New FastAPI endpoints** back all of the above: `POST /run`, `POST /catalog`, `POST /publish`
— each returns `202` and runs as a background task, so Port's call never blocks on a scrape.

**Failures are now visible in Port.** `scrape_run` entities record `status = "error"` plus an
`error_detail` field carrying Bright Data's actual error text, so `wf_heal_on_bad_run` can
react and an operator can see *why* a run failed on the dashboard. Previously a failed run was
invisible in Port — it simply produced no rows.

`make run` / `make catalog` / `make rank` / `make publish` remain as **developer
conveniences** for local iteration, but the operating model is Port scheduling and reacting.
The human gates are unchanged: fixture freeze, PR merge, and heal approval on a failed
fixture diff.

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

Drift rules for guide data (7 deterministic checks): rows-per-page out of band ·
required-field null rate · `start_raw` present but `start_utc` parsed to null · **channel
collapse** (< 80% of the requested channels yielded rows) · empty-page rate · schema hash
change · **lineup disappearance** (previously-seen channels vanishing; growth is normal — a
widened scope or new carriage). Drift state is keyed per `collector:provider`, so providers
sharing the collector never compare against each other's history.

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

The catalog scraper has **no fixture** — the Philo catalog is a moving target by design, so
it's gated by a row-count band (`catalog.MIN_ROWS`) instead.

---

## Observability

Three signals, on both the pipeline and the API endpoints.

**Traces:** `scrape.run` → one `scrape.fetch` per channel page (carrying
`target.provider`, `channel.id`, `channel.rows`, `fetch.mode`, and `relay.cache_hit`) →
`scrape.extract` / `scrape.validate` / `scrape.heal.reprompt` / `scrape.verify.fixture`,
plus `catalog.refresh`, `rank.compute` (with `rank.titles_considered` /
`rank.titles_matched`), `tmdb.lookup`, `port.upsert`, and the workforce spans
`factory.build` and `builder.feature`. Running under `opentelemetry-instrument` also traces
FastAPI, so **Port's automation calling `/heal` appears as a span** — the integration is in
the trace, not just this README.

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
case. `scripts/bootstrap_signoz.py` provisions the dashboard, alert, and webhook channel.

---

## Limitations — what's real and what isn't

- **The factory still runs on the operator's machine.** Port workflows (cron + event) now
  drive the cycle by calling the FastAPI endpoints (`/run`, `/catalog`, `/publish`, `/heal`),
  but those endpoints execute locally — Bright Data's streaming-domain policy and the
  self-hosted SigNoz keep the actual scraping on the laptop, reachable via the tunnel. Port
  operates the factory; it does not yet host it.
- **Bright Data policy blocks collector runs against streaming-media domains** (their
  support confirmed and endorsed the fetch/extract split). Consequences: runs in the
  Bright Data console show relay-tunnel URLs (the *content* is live — the Web Unlocker
  fetched it seconds earlier), and the factory depends on the fixtures tunnel being up.
- **Quick-tunnel URLs rotate on every restart** (`trycloudflare.com`). After restarting a
  tunnel, update `FIXTURE_BASE_URL` / `HEAL_WEBHOOK_URL` / `BUILD_WEBHOOK_URL` /
  `FEATURE_WEBHOOK_URL` in `.env` and re-run `make bootstrap` so Port points at the new URL.
- **The catalog scraper has no fixture gate** — the Philo catalog is a moving target by
  design, so it's gated by a row-count band instead (`catalog.MIN_ROWS`). And because
  philo.com is a JS SPA, the collector path yields nothing from static copies — the
  embedded-JSON parser is what delivers in practice.
- **Catalog coverage is partial**: the browse pages render a large subset of Philo's full
  library, so some ranked titles fall back to guide links instead of direct record pages.
- **TMDB is optional**: without a key the ranking degrades to guide heuristics (airing
  volume, prime time, NEW badges) and is noticeably worse.
- **streamingtvguides.com is a third-party aggregator** — guide accuracy (and the
  Eastern-time display the local UTC conversion assumes) is theirs.
- **The Triage agent authors drift descriptions**; deeper reasoning over long run histories
  is a work in progress.
- **One Port UI step isn't scripted**: embedding the SigNoz dashboard as an iframe widget
  on Factory Operator. Everything else — blueprints, automations, actions, scorecard,
  operator dashboard, the AI agents, SigNoz dashboard/alert/channel — is provisioned by
  `bootstrap_port.py` and `bootstrap_signoz.py`.
- **The Vercel git-metadata seat block** (`TEAM_ACCESS_REQUIRED`) affects CI-triggered
  deploys on a free team; being addressed in `deploy.yml`.
- **No retry/backoff on Bright Data calls.** A transient CLI failure surfaces as an error
  (mitigated for fetches by the 30-minute relay cache, `RELAY_CACHE_MINUTES`).
