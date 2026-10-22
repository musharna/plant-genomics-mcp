#!/usr/bin/env bash
# The 2026-10-20 model-tier sweeps (PREREG_2026-10-20_model-tiers.md): Haiku 5.5,
# then Sonnet 5.5, each 3 reps of bare, web and mcp on the 2026-10-18 key. After
# each tier, rows the API ended before an answer are moved aside and re-run once.
# A usage stop (seven-day >= 0.90) ends the script with the rows kept.
#
#   flock <sweep lock> env HEAVY_RUN_MEM=6G heavy-run bash benchmarks/agent_qa/tiers_sweep.sh [TIER...]
#
# TIER is haiku or sonnet (default: both, Haiku first).
set -euo pipefail
cd "$(dirname "$0")/../.."
A="$HOME/plant-genomics-mcp-artifacts/agent_qa"
K="$A/rerun-2026-10-18/questions.json"
R="$A/tiers-2026-10-20"
free_kb=$(df -Pk "$HOME" | awk 'NR == 2 {print $4}')
if [ "$free_kb" -lt $((5 * 1024 * 1024)) ]; then
        echo "STOP: only $((free_kb / 1024)) MB of disk free, under 5 GB" >&2
        exit 3
fi
tiers=("$@")
[ ${#tiers[@]} -eq 0 ] && tiers=(haiku sonnet)
for tier in "${tiers[@]}"; do
        case "$tier" in haiku | sonnet) ;; *) echo "unknown tier: $tier" >&2; exit 2 ;; esac
        model="claude-$tier-5-5"
        out="$R/$tier"
        mkdir -p "$out"
        args=(--key "$K" --model "$model" --server plant-genomics-mcp==1.29.3
                --exclude-newer 2026-10-17T01:00:00Z --mcp-timeout-ms 180000 --stop-at 0.90 --accept-cli-change
                --arms "bare,web,mcp" --reps 3 --workers 2
                --results "$out/results.jsonl" --runs "$out/runs")
        echo "== $tier start $(date -Is)"
        uv run python benchmarks/agent_qa/run.py "${args[@]}"
        uv run python benchmarks/agent_qa/run.py --key "$K" --results "$out/results.jsonl" --drop-api-failures
        uv run python benchmarks/agent_qa/run.py "${args[@]}"
        echo "== $tier done $(date -Is)"
done
