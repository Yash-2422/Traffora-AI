#!/usr/bin/env bash
# Runs the three comparison scenarios under identical demand and seed.
set -u
cd "$(dirname "$0")/.."
mkdir -p runs
COMMON="--end 900 --scale 2 --seed 42"

echo "=== fixed-time (agents observe, no signal writes) ==="
python -m backend.orchestrator.scripts.run_orchestrator $COMMON --no-signal-control \
  --json-out runs/cmp_fixed.json > runs/cmp_fixed.log 2>&1
echo "exit=$?"

echo "=== traffic-mind only ==="
python -m backend.orchestrator.scripts.run_orchestrator $COMMON --no-emergency --no-parking \
  --json-out runs/cmp_traffic.json > runs/cmp_traffic.log 2>&1
echo "exit=$?"

echo "=== full Traffora-AI ==="
python -m backend.orchestrator.scripts.run_orchestrator $COMMON \
  --json-out runs/cmp_full.json > runs/cmp_full.log 2>&1
echo "exit=$?"
