#!/bin/bash
# run-pending.sh — processes any PDFs in input/ that don't have output yet
# Cron: */30 * * * * /root/projects/prop-capture/run-pending.sh >> /root/logs/prop-capture-cron.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT_DIR="$SCRIPT_DIR/input"
OUTPUT_DIR="$SCRIPT_DIR/output"
PYTHON="python3"

source /root/.secrets.env 2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] prop-capture auto-run starting"

if [ ! -d "$INPUT_DIR" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Input dir not found: $INPUT_DIR — exiting"
    exit 0
fi

PROCESSED=0
SKIPPED=0

for pdf in "$INPUT_DIR"/*.pdf; do
    [ -f "$pdf" ] || continue
    stem=$(basename "$pdf" .pdf)
    out_dir="$OUTPUT_DIR/$stem"

    if [ -f "$out_dir/plan_data.json" ]; then
        echo "  SKIP: $stem (already processed)"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    echo "  RUN: $stem"
    cd "$SCRIPT_DIR"
    $PYTHON pipeline.py "$pdf" --output-dir "$OUTPUT_DIR" && echo "  DONE: $stem" || echo "  FAIL: $stem"
    PROCESSED=$((PROCESSED + 1))
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done — processed: $PROCESSED, skipped (already done): $SKIPPED"
