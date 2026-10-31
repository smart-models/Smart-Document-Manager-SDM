#!/usr/bin/env bash
# Replace {{REPO}} before running. Auth only from the environment; never reads .env.
set -eu
API="${OCR_API_URL:-http://127.0.0.1:8020}"
AUTH=()
if [ -n "${UPSTREAM_CLIENT_TOKEN:-}" ]; then
  AUTH=(-H "Authorization: Bearer $UPSTREAM_CLIENT_TOKEN")
fi
curl -fsS -o /dev/null "${AUTH[@]}" -F "file=@{{REPO}}/calibration/plain_text.png;filename=_eval_delete.png" "$API/ocr"
