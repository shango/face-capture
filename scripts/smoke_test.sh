#!/usr/bin/env bash
# End-to-end smoke test against a running face-capture API.
#
# Usage:
#   API_URL=http://localhost:8000 API_KEY=... ./scripts/smoke_test.sh path/to/test.mp4
#
# Defaults:
#   API_URL=http://localhost:8000
#   API_KEY=$API_KEY    (loaded from your shell or .env)
#   VIDEO=test.mp4      (or first positional arg)
#   TIMEOUT=900         (max seconds to wait for the job)
#
# Exits non-zero with a clear message if anything fails. On success, prints
# the timing breakdown and a summary of bundle contents.

set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
VIDEO="${1:-${VIDEO:-test.mp4}}"
TIMEOUT="${TIMEOUT:-900}"
POLL_INTERVAL="${POLL_INTERVAL:-2}"

if [ -z "${API_KEY:-}" ]; then
  echo "FAIL: API_KEY env var is required." >&2
  exit 1
fi
if [ ! -f "$VIDEO" ]; then
  echo "FAIL: video file not found: $VIDEO" >&2
  exit 1
fi
for cmd in curl unzip python3; do
  command -v "$cmd" >/dev/null || { echo "FAIL: $cmd not on PATH" >&2; exit 1; }
done

# Pull a JSON field out of stdin using Python (no jq dependency).
# Usage: echo "$json" | jget .id
jget() {
  python3 -c '
import json, sys
path = sys.argv[1].lstrip(".").split(".") if sys.argv[1] != "." else []
v = json.load(sys.stdin)
for p in path:
    v = v.get(p) if isinstance(v, dict) else None
print("" if v is None else v)
' "$1"
}

echo "==> Smoke test: $API_URL"
echo "    video: $VIDEO ($(stat -c %s "$VIDEO" 2>/dev/null || stat -f %z "$VIDEO") bytes)"

# 1. Health check ------------------------------------------------------------
echo "==> GET /health"
if ! curl -fsS "$API_URL/health" >/dev/null; then
  echo "FAIL: /health did not return 2xx" >&2
  exit 1
fi
echo "    ok"

# 2. Auth gate sanity check --------------------------------------------------
echo "==> GET /api/jobs/00000000-0000-0000-0000-000000000000 without key (expect 401/403)"
status=$(curl -s -o /dev/null -w '%{http_code}' \
  "$API_URL/api/jobs/00000000-0000-0000-0000-000000000000")
if [ "$status" != "401" ] && [ "$status" != "403" ]; then
  echo "FAIL: expected 401/403 without key, got $status" >&2
  exit 1
fi
echo "    auth gate returns $status as expected"

# 3. Upload ------------------------------------------------------------------
# Match what a browser sends: derive the MIME from the file extension.
# Curl defaults multipart parts to application/octet-stream otherwise, which
# the backend correctly rejects with 415.
case "${VIDEO##*.}" in
  mp4)            mime="video/mp4" ;;
  mov|qt)         mime="video/quicktime" ;;
  avi)            mime="video/x-msvideo" ;;
  mkv)            mime="video/x-matroska" ;;
  webm)           mime="video/webm" ;;
  m4v)            mime="video/x-m4v" ;;
  *)              mime="video/mp4" ;;
esac

echo "==> POST /api/jobs (Content-Type: $mime)"
t_upload_start=$(date +%s)
upload_response=$(curl -fsS \
  -H "X-API-Key: $API_KEY" \
  -F "video=@${VIDEO};type=${mime}" \
  "$API_URL/api/jobs")
t_upload_end=$(date +%s)
job_id=$(echo "$upload_response" | jget .id)
job_status=$(echo "$upload_response" | jget .status)
if [ -z "$job_id" ] || [ "$job_id" = "null" ]; then
  echo "FAIL: no job id in upload response: $upload_response" >&2
  exit 1
fi
echo "    job_id=$job_id status=$job_status upload_time=$((t_upload_end - t_upload_start))s"

# 4. Poll until terminal -----------------------------------------------------
echo "==> Polling /api/jobs/$job_id every ${POLL_INTERVAL}s (timeout ${TIMEOUT}s)"
t_poll_start=$(date +%s)
final_status=""
while true; do
  detail=$(curl -fsS -H "X-API-Key: $API_KEY" "$API_URL/api/jobs/$job_id")
  status=$(echo "$detail" | jget .status)
  elapsed=$(( $(date +%s) - t_poll_start ))
  echo "    [${elapsed}s] status=$status"
  case "$status" in
    succeeded|failed)
      final_status="$status"
      break
      ;;
  esac
  if [ "$elapsed" -ge "$TIMEOUT" ]; then
    echo "FAIL: timed out after ${TIMEOUT}s; last detail: $detail" >&2
    exit 1
  fi
  sleep "$POLL_INTERVAL"
done

if [ "$final_status" != "succeeded" ]; then
  err=$(echo "$detail" | jget .error_log)
  [ -z "$err" ] && err="(no error_log)"
  echo "FAIL: job ended in status=$final_status" >&2
  echo "      error_log:" >&2
  echo "$err" >&2
  exit 1
fi
processing_time=$(( $(date +%s) - t_poll_start ))
echo "    succeeded after ${processing_time}s"

# 5. Bundle URL + download ---------------------------------------------------
echo "==> GET /api/jobs/$job_id/bundle"
bundle_response=$(curl -fsS -H "X-API-Key: $API_KEY" "$API_URL/api/jobs/$job_id/bundle")
bundle_url=$(echo "$bundle_response" | jget .url)
expires_in=$(echo "$bundle_response" | jget .expires_in)
if [ -z "$bundle_url" ] || [ "$bundle_url" = "null" ]; then
  echo "FAIL: no signed url in bundle response: $bundle_response" >&2
  exit 1
fi
echo "    url=$bundle_url expires_in=${expires_in}s"

# Resolve relative URLs (LocalStorage backend returns /storage/...).
case "$bundle_url" in
  http://*|https://*) absolute_url="$bundle_url" ;;
  *)                  absolute_url="${API_URL%/}${bundle_url}" ;;
esac

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
zip_path="$tmp/bundle.zip"
echo "==> Downloading bundle"
curl -fsS -o "$zip_path" "$absolute_url"
size=$(stat -c %s "$zip_path" 2>/dev/null || stat -f %z "$zip_path")
echo "    downloaded $size bytes"

# 6. Verify contents ---------------------------------------------------------
echo "==> Inspecting bundle"
contents=$(unzip -Z1 "$zip_path" | sort)
echo "$contents" | sed 's/^/    /'

required_files="apply_in_maya.py
blendshapes.csv
preview.mp4
README.txt"

missing=""
for f in $required_files; do
  if ! echo "$contents" | grep -qx "$f"; then
    missing="$missing $f"
  fi
done
if [ -n "$missing" ]; then
  echo "FAIL: bundle missing required file(s):$missing" >&2
  exit 1
fi

# 7. CSV sanity --------------------------------------------------------------
unzip -p "$zip_path" blendshapes.csv > "$tmp/blendshapes.csv"
header=$(head -1 "$tmp/blendshapes.csv")
columns=$(echo "$header" | awk -F',' '{print NF}')
rows=$(($(wc -l < "$tmp/blendshapes.csv") - 1))
echo "    blendshapes.csv: $columns columns, $rows data rows"

# ARKit-52 + timestamp/frame column = 53. Don't be strict on exact count in
# case the column set evolves; just require >= 52 and complain on NaN.
if [ "$columns" -lt 52 ]; then
  echo "FAIL: blendshapes.csv has $columns columns (<52)" >&2
  exit 1
fi
if grep -qi 'nan' "$tmp/blendshapes.csv"; then
  echo "FAIL: blendshapes.csv contains NaN values" >&2
  exit 1
fi

# 8. Summary -----------------------------------------------------------------
echo
echo "==> PASS"
echo "    job_id           $job_id"
echo "    upload           $((t_upload_end - t_upload_start))s"
echo "    pipeline         ${processing_time}s"
echo "    bundle size      $size bytes"
echo "    csv columns      $columns"
echo "    csv rows         $rows"
