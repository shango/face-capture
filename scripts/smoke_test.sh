#!/usr/bin/env bash
# End-to-end smoke test against a running face-capture API.
#
# Usage:
#   API_URL=http://localhost:8000 ./scripts/smoke_test.sh path/to/test.mp4
#
# Defaults:
#   API_URL=http://localhost:8000
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

# Deliverables are named after the uploaded clip; mirror the backend's
# _safe_clip_stem so we can assert the bundle layout.
_vbase=$(basename "$VIDEO")
clip_stem=$(printf '%s' "${_vbase%.*}" \
  | sed -E 's/[^A-Za-z0-9_-]+/_/g; s/^[_-]+//; s/[_-]+$//')
[ -z "$clip_stem" ] && clip_stem="clip"

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

# 2. Unknown job returns 404 --------------------------------------------------
echo "==> GET /api/jobs/00000000-0000-0000-0000-000000000000 (expect 404)"
status=$(curl -s -o /dev/null -w '%{http_code}' \
  "$API_URL/api/jobs/00000000-0000-0000-0000-000000000000")
if [ "$status" != "404" ]; then
  echo "FAIL: expected 404 for unknown job id, got $status" >&2
  exit 1
fi
echo "    unknown job returns 404 as expected"

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
  detail=$(curl -fsS "$API_URL/api/jobs/$job_id")
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
bundle_response=$(curl -fsS "$API_URL/api/jobs/$job_id/bundle")
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

required_files="${clip_stem}/${clip_stem}_apply_in_maya.py
${clip_stem}/${clip_stem}_apply_in_blender.py
${clip_stem}/${clip_stem}_blendshapes.csv
${clip_stem}/${clip_stem}_preview.mp4
${clip_stem}/${clip_stem}_README.txt"

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

# 6b. USD bundle assets (optional — skipped if the one-off Maya export has
#     not been run yet, see scripts/export_arkit_head_to_usd.py). When the
#     asset is present on the server, the bundle MUST contain both files
#     and the animated .usda MUST open as a USD stage.
usd_head="${clip_stem}/${clip_stem}_arkit_head.usdc"
usd_animated="${clip_stem}/${clip_stem}_animated.usda"
have_head=$(echo "$contents" | grep -cx "$usd_head" || true)
have_animated=$(echo "$contents" | grep -cx "$usd_animated" || true)
if [ "$have_head" = "1" ] && [ "$have_animated" = "1" ]; then
  echo "==> USD assets present — validating"
  unzip -p "$zip_path" "$usd_animated" > "$tmp/animated.usda"
  unzip -p "$zip_path" "$usd_head"     > "$tmp/arkit_head.usdc"
  if ! python3 -c "
from pxr import Usd, UsdSkel
s = Usd.Stage.Open('$tmp/animated.usda')
assert s, 'failed to open animated.usda'
anims = [p for p in s.Traverse() if p.IsA(UsdSkel.Animation)]
assert anims, 'no UsdSkelAnimation prim in animated.usda'
attr = UsdSkel.Animation(anims[0]).GetBlendShapeWeightsAttr()
n = len(attr.GetTimeSamples())
assert n > 0, 'no blendShapeWeights time samples'
print(f'    UsdSkelAnimation OK ({n} samples)')
" 2>&1; then
    echo "FAIL: USD validation failed" >&2
    exit 1
  fi
elif [ "$have_head" = "0" ] && [ "$have_animated" = "0" ]; then
  echo "==> USD assets not present (run scripts/export_arkit_head_to_usd.py to enable)"
else
  echo "FAIL: partial USD bundle — one file present without the other" >&2
  echo "      head=$have_head animated=$have_animated" >&2
  exit 1
fi

# 7. CSV sanity --------------------------------------------------------------
unzip -p "$zip_path" "${clip_stem}/${clip_stem}_blendshapes.csv" > "$tmp/blendshapes.csv"
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
