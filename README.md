# The Scraper Factory

**What should I record this week?** The Scraper Factory scrapes ~12 days of TV-guide data per
channel for Philo, YouTube TV, and Sling, ranks the **top 10 upcoming shows and top 10
upcoming movies** by TMDB popularity, and gives you one click to record each title on Philo.

That is the product. The engineering thesis underneath it is the real point: **web scrapers
rot.** A guide site ships a redesign, selectors stop matching, and the data either stops
arriving or — worse — arrives subtly wrong. This project treats that as a reliability problem
with an on-call rotation. Scrapers are catalogued services with health states. Every run is
traced. Drift is an alert. Repair is an automated loop that must prove itself against a frozen
fixture before its output is trusted, and escalates to a human when it can't.

The same doctrine now runs the whole factory. **Port operates it** (schedules the scrapes,
reacts to failures, hosts the operator UI and the AI agents), **Bright Data** does the
scraping and the self-healing, and **SigNoz** carries every trace, metric, and log. The one
rule that ties it together:

> **Judgment goes to the agents. Proof stays deterministic.** An LLM decides *what* broke and
> *how* to repair it. A plain-code verifier, diffing against a frozen fixture, decides whether
> the repair is *real*. A gate you can't trust deterministically isn't a gate.

**Live webapp:** <https://scraper-factory-top20.vercel.app>
**Live factory service** (demo site + `/top20` + `/api/top20`, tunneled from the demo
machine — up while the factory laptop runs; quick-tunnel URLs rotate on restart):
<https://tree-doc-shuttle-ordered.trycloudflare.com>
**The factory's own front door:** `GET /` on the running service (serves the demo site below)
**Demo walkthrough:** [`demo/index.html`](demo/index.html) — a self-contained multipage site
(story · architecture diagrams · agent workforce · platform), served by the factory itself at
`/` and also openable as a plain file.

---

## The three loops

Everything in this repo is one of three loops. They share the control plane, the telemetry,
and the "judgment vs. proof" split.

### 1 · The daily production loop — Port operates the factory

Port drives the pipeline on a schedule and reacts to what comes back. No human presses a
button in the happy path.

```
Port cron ─► POST /catalog (07:30 UTC) ─► refresh Philo record-link map
Port cron ─► POST /run     (08:00 UTC) ─► scrape guide, all providers ─► scrape_run entity
                                                                              │
                    ┌─────────────────────────────────────────────────────────┤
       success, no drift ▼                                        error / drift ▼
   wf_publish_on_success ─► POST /publish ─► rank ─► Context Lake ─► webapp   wf_heal_on_bad_run ─► POST /heal  (loop 2)
```

- **Scheduled workflows (cron, UTC):** `wf_scheduled_catalog` (07:30 → `/catalog`),
  `wf_scheduled_scrape` (08:00 → `/run`).
- **Event workflows (react to `scrape_run` entities):** `wf_publish_on_success`
  (`status = success` → `/publish`), `wf_heal_on_bad_run` (`status = error`/`drift` →
  `/heal`, with the Triage agent authoring the description).
- **Operate-now buttons** for a human who wants to force a cycle: `run_scrape_now`,
  `refresh_catalog_now`, `publish_now`.

This loop is **end-to-end proven, not aspirational.** A Port `run_scrape_now` button called
`/run`, produced 650 rows with no drift, and `wf_publish_on_success` fired `/publish` *from
Port's cloud* to refresh the lake — the operator never touched the laptop.

### 2 · The self-heal loop — repair, gated by a deterministic verifier

When a `scrape_run` lands in error or drift, `/heal` runs the repair — and the fixture
arbitrates whether it worked.

```
/heal ─► brightdata scraper heal (semantic symptom prompt) ─► preview_result
                                                                    │
                                                    Verifier: diff vs frozen fixture  ◄─ deterministic code
                                              pass ▼                         ▼ fail
                                       approve → healthy          --reject → broken → human queue
```

- **Two triggers, one idempotent endpoint.** The inline detector sets Port `health = drifting`
  in seconds (fast path); the SigNoz alert webhook arrives ~5 minutes later (safety net, and
  the only thing that catches a pipeline that died entirely). Whichever fires first does the
  work; the other finds it already healthy.
- **`/heal` is robust to messy payloads.** If a trigger delivers unresolved template fields,
  `/heal` resolves the newest error/drift `scrape_run` from the Context Lake itself (run
  identifiers encode the provider; the `scraper` relation carries the collector id).
- **The verifier is plain code.** It diffs the heal preview against a frozen fixture; nothing
  is ever promoted without a passing diff.

This loop is **also proven, including the unhappy path — which is the point.** A fault-injected
failure produced an error `scrape_run` (with `error_detail`), `wf_heal_on_bad_run` fired, the
Triage agent authored a drift description, `/heal` ran a real Bright Data heal cycle, and the
deterministic verifier **rejected the repair** (2 rows against the fixture's 30) and escalated
to a human. The next clean run self-cleared health back to healthy. **A rejected bad repair is
the system working correctly** — it is exactly what prevents a broken scraper from silently
poisoning the product.

### 3 · The software-change loop — a feature request becomes a deployed PR

The same "judgment to agents, proof stays deterministic" split, applied to shipping code.

```
Port action submit_feature_request ─► POST /feature ─► requirement (building)
   ─► local Claude Code edits a scratch clone (file tools only)
   ─► deterministic git: branch req/req_<id> · commit · push · open PR
   ─► GitHub Actions ci.yml (pytest + tsc + next build — never an LLM)
   ─► human reviews & merges
   ─► deploy.yml ─► Vercel deploy + upsert factory_deployment + flip requirement → deployed
```

**The builder runs locally on purpose: no Anthropic credentials ever leave the operator's
machine.** GitHub Actions runs only no-AI steps. The builder edits files; branch/commit/push/PR
mechanics stay in deterministic code, which also guarantees the PR-body contract
(`Requirement: req_<id>`, first line) that `deploy.yml` parses on merge. The builder is
forbidden from touching `fixtures/`, `src/factory/agents/verifier.py`, and the drift thresholds
in `src/factory/detect.py`, and the run aborts if the resulting diff touches any of them. The
human merge and the deterministic CI are the promotion gates.

---

## Quickstart

**Prerequisites:** Python 3.11+, Node 20+, Docker, a Bright Data API key, a Port account,
a SigNoz instance, and (optional but recommended) a free TMDB API key.

```bash
git clone <this-repo> && cd scraper-factory
cp .env.example .env          # fill in credentials (see the secrets map below)

# SigNoz (self-hosted, recommended — no key, no trial clock)
git clone -b main https://github.com/SigNoz/signoz.git ../signoz
docker compose -f ../signoz/deploy/docker/docker-compose.yaml up -d
# UI at http://localhost:8080, OTLP at http://localhost:4318

make setup                    # deps + OTel auto-instrumentation
make bootstrap                # Port: blueprints, workflows, actions, scorecard, dashboard, agents (idempotent)
python3 scripts/bootstrap_signoz.py   # SigNoz: dashboard + drift alert + webhook channel (idempotent)
make serve                    # factory API + demo site on :8000, auto-instrumented

# Once provisioned, Port drives the loop. To exercise it by hand (dev conveniences):
make run                      # scrape ~7 days of channel pages, all providers
make catalog                  # map titles -> Philo record URLs (shows + movies)
make rank                     # compute the top 10 shows + top 10 movies
make publish                  # push the ranking to Port's Context Lake
make demo-drift               # manufacture drift and watch the repair loop
make verify                   # fixture gate; non-zero exit on mismatch
```

With the service up, open **<http://localhost:8000/>** — that's the demo site, served by the
factory. `make run` / `catalog` / `rank` / `publish` are developer conveniences.

### Production path — weekly, laptop-free

In operation nothing runs on the laptop. Port's `wf_scheduled_scrape` workflow (cron, Mondays
08:00 UTC) dispatches the **`weekly-scrape` GitHub Actions workflow**, which runs the whole
pipeline inside the runner: scrape all providers (7-day window) → heal-if-drifted (same
Bright Data heal → deterministic fixture-verify → approve/reject contract) → catalog refresh →
rank + publish. The runner serves `fixtures/` to Bright Data's cloud through a job-scoped
`cloudflared` quick tunnel, so no standing tunnel exists. The `run_scrape_now` self-service
action in Port dispatches the same workflow on demand. A GitHub `schedule` fallback fires two
hours after Port's slot and skips itself if a Port-triggered run already succeeded that week.

One-time setup:

- **GitHub repo secrets**: `BRIGHTDATA_API_KEY`, `PORT_CLIENT_ID`, `PORT_CLIENT_SECRET`,
  `TMDB_API_KEY`. **Repo variables**: `SCRAPER_STUDIO_COLLECTOR_ID_CHANNEL`,
  `SCRAPER_STUDIO_COLLECTOR_ID_PHILO_CATALOG`. (Per the standing rule, no `ANTHROPIC_API_KEY`
  in CI — the Builder stays laptop-only.)
- **Port org secret `github-pat`**: a fine-grained GitHub PAT scoped to this repo with
  *Actions: write* (Port UI → Credentials → Secrets). Port's workflow/action definitions
  reference it as `{{ .secrets["github-pat"] }}`; it never appears in plaintext config.
- `make bootstrap` to push the workflow/action definitions to Port.

### Dev/demo path — tunnels

For local demos, Port's cloud must reach the factory's webhook endpoints, and Bright Data's
cloud collectors must reach the frozen fixtures — neither can see `localhost`. Expose both
with `cloudflared` and point the env vars at the printed URLs:

```bash
cloudflared tunnel --url http://localhost:8000   # factory: set *_WEBHOOK_URL (+ token) before `make bootstrap`
make serve-fixtures                              # fixtures/ on :9000
cloudflared tunnel --url http://localhost:9000   # collectors: set FIXTURE_BASE_URL
```

Quick-tunnel URLs (`trycloudflare.com`) rotate on every restart; update the env vars and
re-run `make bootstrap` so Port points at the new URLs.

### First-run setup — the two collectors

Two Bright Data collectors serve everything: **one shared channel-schedule scraper** (every
streamingtvguides channel page has the same structure; providers differ only in lineup) and
the Philo catalog scraper.

```bash
brightdata login --api-key $BRIGHTDATA_API_KEY
# catalog scraper: created directly against the live page
brightdata scraper create "https://www.philo.com/go/allshows" "<PROMPT from src/factory/catalog.py>"
```

Creating the **channel collector** requires a known dance (Bright Data's AI generation fails
on full-size pages and bakes in a bad crawl if the sample has followable links) — create it
against the trimmed fixture served through the tunnel:

```bash
brightdata scraper create "$FIXTURE_BASE_URL/channel.html" "<PROMPT from src/factory/pipeline.py>"
# then: run it on the fixture, hand-check the rows, freeze fixtures/channel.expected.json
# pin both collector_ids in .env AND CLAUDE.md (full steps: CLAUDE.md "known dance")
```

---

## The data

Every channel on streamingtvguides.com has its own page (`/Channel/<ID>`) — a flat list of
**~455 program cards spanning ~12 days**. One shared collector extracts any of them; the
provider → channel mapping is parsed deterministically from each provider's guide-page anchors
(Philo 137 channels, YouTube TV 175, Sling 119). A run scrapes `GUIDE_MAX_CHANNELS` pages per
provider (default 25); times display in Eastern and are converted to UTC locally, never by the
scraper. Watch spend with `brightdata budget`.

Philo record links come from a second scrape. `philo.com/go/allshows` links every show to its
player page (`philo.com/player/show/<id>`), and `philo.com/go/allmovies` does the same for
movies — an additive merge into one catalog (~739 titles total). Those IDs are internal base64
blobs, so scraping is the only way to get them. philo.com is a JS-rendered SPA, so when the
collector sees an empty static copy, `catalog._parse_embedded()` falls back to the show IDs
embedded as JSON in the fetched page. The ranker joins guide and catalog on normalized title.

**The fetch/extract split.** Bright Data policy blocks collector runs against streaming-media
domains (confirmed and endorsed by their support). So scraping is a two-product pipeline: the
**Web Unlocker** (`brightdata scrape`) fetches the live page and archives it under
`fixtures/relay/`, and the **Scraper Studio collector** extracts from that tunneled copy. The
code always tries a direct collector run first, so if policy ever lifts the relay disappears by
itself — and every scrape's input HTML is archived, making drift investigations replayable
byte-for-byte.

**Ranking** = 0.6·TMDB popularity + 0.25·TMDB rating + 0.1·airing volume, plus boosts for
NEW/premiere badges and prime-time slots, computed **within each kind** so the output is a
top-10 of shows and a top-10 of movies rather than one blended top-20. TMDB responses are
cached in SQLite, so re-ranking is free and offline; without a key the ranking degrades to
guide-only heuristics.

---

## The webapp — a Context Lake consumer

`webapp/` is a Next.js app showing the ranking as two side-by-side columns — **Shows on the
left, Movies on the right** — each card linking to the title's Philo page, where **Record** is
one click for a logged-in subscriber. It has **no database of its own**: it reads the
`ranked_title` entities (`show_01..10`, `movie_01..10`) straight from Port's Context Lake, the
same lake the operator dashboard and the AI agents use. The `/publish` step is the seam; the
route renders per request so it always reflects the live lake. Without reachable Port
credentials it renders sample data behind a visible banner, so the demo never white-screens.

Live at **<https://scraper-factory-top20.vercel.app>**. For local dev, put `PORT_CLIENT_ID` /
`PORT_CLIENT_SECRET` / `PORT_API_BASE` in `webapp/.env.local` and run `npm install && npm run
dev`. Deploys happen automatically on merge via `deploy.yml`; a manual Vercel import (Root
Directory `webapp/`, the three `PORT_*` env vars) also works.

---

## Endpoints

The FastAPI service exposes the API and serves the demo site from one origin. The six POST
endpoints are token-gated; the GET routes are open.

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /run` | token | scrape one/all providers in the background (202) |
| `POST /catalog` | token | refresh the Philo record-link catalog (202) |
| `POST /publish` | token | rank + push the top-10s to the Context Lake (202) |
| `POST /heal` | token | run the self-heal loop (Port automation + SigNoz alert) |
| `POST /feature` | token | turn a requirement into a PR via the local builder |
| `POST /build` | token | a brief in, a verified scraper out |
| `GET /` (+ `/architecture.html`, `/workforce.html`, `/platform.html`) | open | the demo site — the factory's front door |
| `GET /top20` | open | the product: clickable top-20 upcoming titles |
| `GET /api/top20` | open | the same ranking as JSON |
| `GET /healthz` | open | liveness |
| `GET /docs` | open | FastAPI's interactive OpenAPI docs |

**Webhook auth.** A shared secret `FACTORY_WEBHOOK_TOKEN` gates all six POST endpoints, carried
as `?token=...` or an `x-factory-token` header and compared in constant time (`hmac.compare_digest`);
a bad or missing token returns `401` and logs a warning. `bootstrap_port.py` bakes the token
into every provisioned Port automation/action/workflow URL via a single `_tokened()` helper,
and `bootstrap_signoz.py` bakes it into the alert channel URL (SigNoz can't send custom headers,
so it rides in the query string). Verified live: a bare `POST` returns `401`; a Port cloud
button drives a tokened call to `200`. When `FACTORY_WEBHOOK_TOKEN` is unset the gate opens, so
local dev without provisioned webhooks still works. The `POST /run|/catalog|/publish` endpoints
return `202` immediately and run as background tasks, so a Port call never blocks on a scrape.

---

## Where every credential lives

No secret ever lives in Port entities or in this repo. Each credential lives in exactly one
place:

| Credential | Lives in | Used by |
|---|---|---|
| `BRIGHTDATA_API_KEY` | local `.env` | the factory's scrapes + heals |
| `TMDB_API_KEY` | local `.env` | the ranker |
| `PORT_CLIENT_ID` / `PORT_CLIENT_SECRET` | local `.env` **and** Vercel env vars **and** GitHub Actions secrets | factory (local), webapp (Vercel), deploy workflow (CI) |
| `PORT_API_BASE` | local `.env` + Vercel env vars | factory + webapp |
| `FACTORY_WEBHOOK_TOKEN` | local `.env` only; **embedded in the webhook URLs** the bootstrap scripts write into Port + SigNoz | the `_require_token` gate |
| `VERCEL_TOKEN` / `VERCEL_ORG_ID` / `VERCEL_PROJECT_ID` | GitHub Actions secrets | `deploy.yml` |
| Anthropic auth | **local machine only** (the builder authenticates locally) | the local Claude Code builder |

**GitHub Actions secrets** (five, names only): `PORT_CLIENT_ID`, `PORT_CLIENT_SECRET`,
`VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`. There is deliberately **no
`ANTHROPIC_API_KEY` in CI** — a cloud-runner builder was prototyped and removed so no Anthropic
credential ever reaches GitHub.

---

## The platforms

### Port — the control plane

| pillar | use |
|---|---|
| **Catalog / Context Lake** | Seven blueprints: `data_source`, `scraper`, `scrape_run`, `heal_event`, `ranked_title`, `requirement`, `factory_deployment`; plus two `service` entities. The 20 `ranked_title` entities are the product. |
| **Actions** | `request_data_source` (brief → verified scraper), `submit_feature_request` (requirement → PR), and the buttons `run_scrape_now` / `refresh_catalog_now` / `publish_now` |
| **Automations & Workflows** | `invoke_healer_on_drift` (`health == "drifting"` → `/heal`); scheduled `wf_scheduled_scrape` / `wf_scheduled_catalog`; event `wf_publish_on_success` / `wf_heal_on_bad_run` |
| **Scorecards** | `scraper_reliability`: Basic → Bronze (verified today) → Silver (verify passing) → Gold (healthy + verified) |
| **Dashboards** | Factory Operator: scrapers-by-health pie, heal-event table, ranked-titles table, architecture note, embedded SigNoz |
| **AI Agents** | Triage, Heal Explainer, On-Call Assistant, Catalog Coverage |

All of it is provisioned by `scripts/bootstrap_port.py` — idempotent, re-runnable, no UI
clicks, validated against the live `api.getport.io`.

**The four AI agents** (registered as `_ai_agent` entities): **Triage** (Approval Required —
reads run + heal history, decides heal vs escalate, authors the drift description), **Heal
Explainer** (Automatic, read-only — narrates why the verifier approved/rejected), **On-Call
Assistant** (Approval Required — operator Q&A; can file a `submit_feature_request`), **Catalog
Coverage** (Automatic, read-only — reports record-link coverage). The workspace also carries
stock Port demo agents; only these four are wired into this system.

### SigNoz — the telemetry

Three correlated signals, on both the pipeline and the FastAPI endpoints. Running under
`opentelemetry-instrument` means even Port's automation calling `/heal` shows up as a span.
`make serve` pins `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` so the exporter matches the OTLP
endpoint.

- **Traces:** `scrape.run` → one `scrape.fetch` per channel page (carrying `target.provider`,
  `channel.id`, `channel.rows`, `fetch.mode`, `relay.cache_hit`) → `scrape.extract` /
  `scrape.validate` / `scrape.heal.reprompt` / `scrape.verify.fixture`, plus `catalog.refresh`,
  `rank.compute`, `tmdb.lookup`, `port.upsert`, and the workforce spans `factory.build` and
  `builder.feature`.
- **Metrics:** `scraper.drift.detected` (the alert), `scraper.heal.attempts` / `success`,
  `scraper.verify.pass`, `scraper.field.null_rate`, `relay.cache` (hit/miss),
  `ranker.catalog_match_rate`, and the per-operation histograms.
- **Logs:** trace-correlated (`trace_id`/`span_id` injected). The drift description, the heal
  prompt, and the verifier's diff are all logged — the audit trail for every automated repair.
- **Alert:** `scraper.drift.detected > 0` over a 5-minute window, checked every minute, plus an
  "alert when data stops coming" rule for the dead-pipeline case. Provisioned (dashboard, alert,
  webhook channel) by `scripts/bootstrap_signoz.py`.

**Two pull bridges.** Most signals are pushed by the pipeline as it runs, but Port's
control-plane activity and Bright Data's account budget are not things those systems push
anywhere — so the factory polls them. A background watcher (`portwatch.py`, started with the
FastAPI service) reads Port's action-run and workflow-run listings every `PORT_WATCH_INTERVAL_S`
seconds and re-emits each new run as the `port.action.runs` / `port.workflow.runs` counters
(labelled by action/workflow and status), and it samples `brightdata budget` into the
`brightdata.budget.remaining` gauge (a scraping-only Bright Data key can't read the balance, so
that gauge stays empty rather than erroring). Both feed the "Port control-plane activity" and
"Bright Data budget remaining" dashboard panels. Runs are de-duplicated in SQLite so a restart
never double-counts.

### Bright Data — the scrapers

| Collector | Target | Promotion gate |
|---|---|---|
| **channel-schedule** (one shared) | any `streamingtvguides.com/Channel/<ID>` page | frozen fixture: `channel.html` + `channel.expected.json` diff |
| **philo-catalog** | `philo.com/go/allshows` (+ `/go/allmovies` via embedded-JSON) | row-count band (`catalog.MIN_ROWS`), no fixture |

The fetch/extract relay split (above) and the `scraper heal` self-repair are the two Bright
Data behaviours the factory is built around.

---

## Architecture invariants

These are the rules the code enforces and that any change must preserve (see also `CLAUDE.md`):

- **The Verifier is deterministic code, never an LLM.** It is the only thing allowed to promote
  a healed scraper, and only on a passing fixture diff.
- **Fixtures are frozen inputs.** Never regenerate `*.expected.json` to make a test pass. A
  bad-repair rejection is a success, not a failure.
- **Two trigger paths, one idempotent `/heal`.** Neither the inline detector nor the SigNoz
  alert may double-promote.
- **Drift vs. real change.** Lineup churn needs two consecutive confirming runs; the fixture
  arbitrates whether the scraper or the guide changed.
- **Drifted runs never feed the ranker.** Guide rows persist only when the drift detector is
  clean.
- **Port writes never raise into the pipeline.** A health/entity write failure must not kill a
  scrape or a repair.
- **Secrets live in `.env`, never in this repo or in Port.** The webhook token rides only in
  the provisioned webhook URLs.

---

## Broken scraper vs. real change, and the drift rules

A drifted scraper and a genuinely changed guide look identical in one sample. Resolved two ways:
**two consecutive confirming runs** before a change (e.g. lineup churn) is accepted as real
(`state.confirm_change`), and **the fixture arbitrates** — if a candidate still reproduces
`expected.json` from frozen HTML, the scraper is healthy and the guide really changed.

The seven deterministic drift rules for guide data: rows-per-page out of band · required-field
null rate · `start_raw` present but `start_utc` parsed to null · **channel collapse** (< 80% of
requested channels yielded rows) · empty-page rate · schema-hash change · **lineup
disappearance** (previously-seen channels vanishing; growth is normal). Drift state is keyed per
`collector:provider`, so providers sharing the collector never compare against each other's
history.

---

## Verification

```
fixtures/
  channel.html            # a REAL captured channel page, trimmed to 30 program cards
  channel.expected.json   # hand-checked scraper output for that exact page
  channel.mutated.html    # same programs and times, every structural handle moved
  relay/                  # archived inputs of every relayed scrape (gitignored)
```

`make verify` scrapes the **tunneled frozen fixture** (`FIXTURE_BASE_URL` — Bright Data's cloud
collectors can't reach localhost) and diffs the output against `channel.expected.json`, exiting
non-zero on mismatch. Without `FIXTURE_BASE_URL` it falls back to a live page and warns, because
a gate against a moving target isn't reproducible. `channel.mutated.html` renames card classes,
reorders cards, and wraps airtimes while keeping every program identical — a selector-based
scraper breaks on it, a semantic prompt heals; `make demo-drift` injects that drift on demand.
The catalog scraper has no fixture (moving target by design) and is gated by `catalog.MIN_ROWS`.

---

## Limitations — what's real and what isn't

- **The production scrape is a weekly GitHub Actions job, not a hosted service.** Port
  dispatches `weekly-scrape.yml` (schedule or the `run_scrape_now` action) and the runner does
  scrape → heal → catalog → publish, then vanishes. Between runs there is no live endpoint:
  SigNoz-alert-driven healing, `/build`, and `/feature` only work when the dev/demo laptop
  service (and its tunnels) is up. Port *operates* the factory; GitHub hosts each run of it.
- **Bright Data policy blocks collector runs against streaming-media domains** (support-endorsed
  fetch/extract split). Runs in the Bright Data console show relay-tunnel URLs (the content is
  live — the Web Unlocker fetched it seconds earlier); the weekly job serves the relay through
  its own job-scoped tunnel, and dev runs depend on the fixtures tunnel being up.
- **Quick-tunnel URLs rotate on restart** (dev/demo path only). Update the `*_WEBHOOK_URL` /
  `FIXTURE_BASE_URL` env vars and re-run `make bootstrap` when they change.
- **Weekly state lives in an Actions cache.** `out/factory.db` is carried between weekly runs
  via `actions/cache` so lineup-churn confirmation (two consecutive runs) works; caches evicted
  after 7 days of disuse just reset that confirmation window, nothing else.
- **The catalog scraper has no fixture gate** — a row-count band instead — and relies on the
  embedded-JSON parser because philo.com is a JS SPA.
- **Catalog coverage is partial**: the browse pages render a subset of Philo's library, so some
  ranked titles fall back to guide links instead of direct record pages.
- **TMDB is optional**: without a key the ranking degrades to guide heuristics and is noticeably
  worse.
- **streamingtvguides.com is a third-party aggregator** — guide accuracy (and the Eastern-time
  display the local UTC conversion assumes) is theirs.
- **The Triage agent authors drift descriptions**; deeper reasoning over long run histories is a
  work in progress.
- **One Port UI step isn't scripted**: embedding the SigNoz dashboard as an iframe widget on
  Factory Operator. Everything else is provisioned by the bootstrap scripts.
- **The Vercel git-metadata gotcha.** On a free/hobby Vercel team, a CI-triggered deploy can hit
  a `TEAM_ACCESS_REQUIRED` seat block if git metadata is attached; being addressed in
  `deploy.yml`. The manual-import path is unaffected.
- **No retry/backoff on Bright Data calls.** A transient CLI failure surfaces as an error
  (mitigated for fetches by the 30-minute relay cache, `RELAY_CACHE_MINUTES`).
```
