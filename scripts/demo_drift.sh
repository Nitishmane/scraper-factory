#!/usr/bin/env bash
# Manufacture drift on demand, then watch the factory repair itself.
#
# This is both the reproducibility proof and the demo centerpiece: a judge can run it
# without you in the room and see the same result.
#
# Bright Data collectors execute in Bright Data's cloud -- they CANNOT fetch localhost or
# local files. The fixtures must be served through a public tunnel, and the scraper is
# pointed at the tunneled mutated page to inject drift.
#
#   terminal 1:  make serve-fixtures          # fixtures/ on :9000
#   terminal 2:  cloudflared tunnel --url http://localhost:9000
#   .env:        FIXTURE_BASE_URL=<the https URL cloudflared prints>
#   then:        ./scripts/demo_drift.sh [provider]
set -euo pipefail

PROVIDER="${1:-philo}"
export PYTHONPATH=src

# Nothing else loads .env for shell runs.
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

bold() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# One shared channel-page fixture gates the guide scraper (see CLAUDE.md).
for f in "fixtures/channel.html" "fixtures/channel.mutated.html" "fixtures/channel.expected.json"; do
  [[ -f "$f" ]] || { echo "missing $f -- freeze fixtures first (see README)"; exit 1; }
done

if [[ -z "${FIXTURE_BASE_URL:-}" ]]; then
  echo "FIXTURE_BASE_URL is not set. Bright Data's cloud cannot reach localhost, so the"
  echo "fixtures must be served through a public tunnel:"
  echo "  terminal 1:  make serve-fixtures"
  echo "  terminal 2:  cloudflared tunnel --url http://localhost:9000"
  echo "  then set FIXTURE_BASE_URL in .env to the https URL cloudflared prints."
  exit 1
fi
FIXTURE_BASE_URL="${FIXTURE_BASE_URL%/}"

MUTATED_URL="${FIXTURE_BASE_URL}/channel.mutated.html"
BASELINE_URL="${FIXTURE_BASE_URL}/channel.html"

if ! curl -fsS --max-time 10 "$MUTATED_URL" >/dev/null; then
  echo "cannot fetch $MUTATED_URL -- is the fixture server up and the tunnel running?"
  echo "  terminal 1:  make serve-fixtures"
  echo "  terminal 2:  cloudflared tunnel --url http://localhost:9000"
  exit 1
fi

bold "1/3  Baseline: scraper against the known-good frozen fixture"
python3 -m factory run "$PROVIDER" --url "$BASELINE_URL" || true

bold "2/3  Injecting drift: scraper against the mutated fixture"
echo "     card classes renamed, cards reordered, times wrapped; the MEANING is unchanged."
echo "     A selector-based scraper breaks here. A semantic prompt should heal."
python3 -m factory run "$PROVIDER" --url "$MUTATED_URL" || true
echo
echo "     The Port write above fires the automation that invokes the healer."
echo "     Watch SigNoz: scraper.rows_returned drops, scraper.drift.detected increments."

bold "3/3  Heal: scraper heal -> preview -> fixture diff -> approve or reject"
echo "     (manual fallback -- in the wired-up demo, Port's automation already did this)"
python3 -m factory heal "$PROVIDER" "channel page restructured: program card classes renamed, cards reordered, airtimes wrapped in new elements" || true

bold "Done"
echo "Check, in order:"
echo "  - Port: the scraper entity's health, and the new heal_event with its trace link"
echo "  - SigNoz: the trace for this run, and the correlated log lines for the repair"
echo "  - a rejected heal leaves health=broken and the entity in a human's queue"
