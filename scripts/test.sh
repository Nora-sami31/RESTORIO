#!/usr/bin/env bash
# Everything that must pass before you demo.
set -euo pipefail
cd "$(dirname "$0")/.."
export RESTORIO_AUTONOMY=full
export RESTORIO_PROVIDER="${RESTORIO_PROVIDER:-offline}"

echo "=== 1/3  unit tests (guardrails, isolation, invariants) ==="
# Runs standalone for a readable report; also collects under pytest,
# where each test asserts so a broken guardrail fails the run.
python3 backend/tests/test_restore.py

echo ""
echo "=== 2/3  acceptance tests — development set ==="
python3 scripts/acceptance.py

echo ""
echo "=== 3/3  held-out tests — unseen scenarios ==="
python3 scripts/heldout.py

echo ""
echo "All suites passed. For the full evidence tables:"
echo "  python3 scripts/ablate.py      # which guardrails earn their place"
echo "  python3 scripts/evaluate.py    # comparison against baselines"
