#!/usr/bin/env bash
# Invoke the deployed function by hand.
#
#   ./scripts/invoke.sh test          # send a test email
#   ./scripts/invoke.sh summary       # send the daily summary now
#   ./scripts/invoke.sh scan          # run a scan now (emails new capacity), then follow its log
#   ./scripts/invoke.sh dry-run       # scan without emailing or saving state, then follow its log
#
# Options: -r REGION, -s STACK (before the mode)
#
# Scans can run for up to 14 minutes, longer than many networks keep an idle
# connection open, so they are invoked asynchronously and followed in the logs.
set -euo pipefail

STACK="cb-monitor"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
while getopts "r:s:h" opt; do
  case "$opt" in
    r) REGION="$OPTARG" ;;
    s) STACK="$OPTARG" ;;
    h) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) exit 1 ;;
  esac
done
shift $((OPTIND - 1))
MODE="${1:-}"

if [[ -z "$REGION" ]]; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi
REGION="${REGION:-us-west-2}"
FUNCTION="${STACK}-monitor"
LOG_GROUP="/aws/lambda/${FUNCTION}"
# Never let the CLI repeat an invocation: that would send duplicate emails.
export AWS_MAX_ATTEMPTS=1

case "$MODE" in
  test)    PAYLOAD='{"mode":"test"}' ;;
  summary) PAYLOAD='{"mode":"summary"}' ;;
  scan)    PAYLOAD='{"mode":"scan"}' ;;
  dry-run) PAYLOAD='{"mode":"scan","dry_run":true}' ;;
  *) echo "usage: $0 [-r REGION] [-s STACK] test|summary|scan|dry-run" >&2; exit 1 ;;
esac

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

if [[ "$MODE" == "test" || "$MODE" == "summary" ]]; then
  aws lambda invoke --region "$REGION" --function-name "$FUNCTION" \
    --cli-binary-format raw-in-base64-out --cli-read-timeout 120 \
    --payload "$PAYLOAD" "$OUT" --query 'StatusCode' --output text >/dev/null
  cat "$OUT"; echo
  exit 0
fi

START_MS=$(( $(date +%s) * 1000 - 5000 ))
aws lambda invoke --region "$REGION" --function-name "$FUNCTION" \
  --invocation-type Event --cli-binary-format raw-in-base64-out \
  --payload "$PAYLOAD" "$OUT" --query 'StatusCode' --output text >/dev/null
echo "Started ${MODE} in ${FUNCTION} (${REGION}). Waiting for it to finish (up to 15 minutes)..."

seen=""
for _ in $(seq 1 90); do
  sleep 10
  lines="$(aws logs filter-log-events --region "$REGION" --log-group-name "$LOG_GROUP" \
    --start-time "$START_MS" \
    --filter-pattern '?"scan finished" ?"holds the lock" ?"published" ?"holding" ?"dry run" ?ERROR ?Traceback' \
    --query 'events[].message' --output text 2>/dev/null || true)"
  if [[ -n "$lines" && "$lines" != "$seen" ]]; then
    printf '%s\n' "${lines#"$seen"}" | cut -c1-2000
    seen="$lines"
  fi
  if grep -qE "scan finished|holds the lock|ERROR|Traceback" <<<"$lines"; then
    exit 0
  fi
done
echo "Still running. Follow it with: aws logs tail ${LOG_GROUP} --region ${REGION} --follow"
