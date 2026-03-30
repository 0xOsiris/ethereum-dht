#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RESULTS_ROOT="/workspace/results"

mkdir -p "$RESULTS_ROOT"

python3 "$ROOT/crawl_discv5.py" --output-dir "$RESULTS_ROOT/discv5" "$@"
python3 "$ROOT/monitor_attestations.py" --output-dir "$RESULTS_ROOT/attestations"
python3 "$ROOT/correlate_validators.py" \
  --crawl-nodes "$RESULTS_ROOT/discv5/nodes.json" \
  --attestations "$RESULTS_ROOT/attestations/attestation_events_expanded.jsonl" \
  --output-dir "$RESULTS_ROOT/correlations"

echo "All outputs written under $RESULTS_ROOT"
