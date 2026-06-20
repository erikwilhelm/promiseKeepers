#!/usr/bin/env bash
# Convenience wrapper for src/extract_promises.py.
#
#   ./run.sh --demo                       # built-in samples
#   ./run.sh --source jsonl:data/sample_rows.jsonl --out output/promises.jsonl
#   ./run.sh                              # no args -> offline smoke test
#
# Any arguments are forwarded verbatim to the extractor, so every command in the
# README works by replacing `python3 extract_promises.py` with `./run.sh`.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
SCRIPT="src/extract_promises.py"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

if [[ $# -gt 0 ]]; then
    exec "$PY" "$SCRIPT" "$@"
fi

# --- no args: offline smoke test (no network, no optional deps) -------------
echo "== 1/2  demo =="
"$PY" "$SCRIPT" --demo

echo
echo "== 2/2  sample corpus -> output/promises_sample.jsonl =="
mkdir -p output
"$PY" "$SCRIPT" \
    --source jsonl:data/sample_rows.jsonl \
    --sections section_1 section_7 \
    --out output/promises_sample.jsonl \
    --min-score 5

echo
echo "Sample output (first rows):"
head -n 3 output/promises_sample.jsonl
