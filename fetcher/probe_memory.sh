#!/usr/bin/env bash
# Render an arbitrary SEQUENCE of URLs in one warm container.
# The point: if "Max Memory Used" is a per-invocation figure it must FALL when
# a heavy page is followed by a light one. If it is a container high-water mark
# it cannot fall, and every "memory is growing" observation ever made from it
# is an artefact of the metric rather than evidence about Chromium.
set -uo pipefail
OUT=$(mktemp -d)
i=0
printf '%-4s %-6s %-9s %-10s %-7s %s\n' run mem_MB dur_ms html_ch status url
for URL in "$@"; do
  i=$((i+1))
  resp=$(aws lambda invoke --function-name schedule-ai-app-fetcher \
    --log-type Tail --cli-binary-format raw-in-base64-out \
    --payload "{\"url\":\"$URL\"}" "$OUT/b-$i.json" 2>/dev/null)
  log=$(echo "$resp" | python3 -c 'import sys,json,base64;print(base64.b64decode(json.load(sys.stdin).get("LogResult","")).decode())')
  mem=$(echo "$log" | grep -oP 'Max Memory Used: \K[0-9]+' | tail -1)
  dur=$(echo "$log" | grep -oP 'Billed Duration: \K[0-9]+' | tail -1)
  init=$(echo "$log" | grep -oP 'Init Duration: \K[0-9.]+' | tail -1)
  read -r chars status < <(python3 - "$OUT/b-$i.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1])); print(d.get("html_chars","?"), d.get("status","?"))
except Exception as e: print("ERR", type(e).__name__)
PY
)
  printf '%-4s %-6s %-9s %-10s %-7s %s%s\n' "$i" "${mem:-?}" "${dur:-?}" "$chars" "$status" \
    "$(echo "$URL" | cut -c1-52)" "${init:+  [COLD ${init}ms]}"
done
echo "bodies in $OUT"
