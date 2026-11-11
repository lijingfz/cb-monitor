#!/usr/bin/env bash
# Remove cb-monitor.
#
#   ./destroy.sh                     # delete the stack (asks for confirmation)
#   ./destroy.sh -y --purge-bucket   # also empty and delete the artifact bucket, no prompt
#
# Options: -r REGION, -s STACK, -y (no prompt), --purge-bucket
set -euo pipefail

cd "$(dirname "$0")"

STACK="cb-monitor"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
YES=0
PURGE=0

while (($#)); do
  case "$1" in
    -r) REGION="$2"; shift 2 ;;
    -s) STACK="$2"; shift 2 ;;
    -y) YES=1; shift ;;
    --purge-bucket) PURGE=1; shift ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$REGION" ]]; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi
REGION="${REGION:-us-west-2}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${STACK}-artifacts-${ACCOUNT}-${REGION}"

echo "This deletes stack '${STACK}' in ${REGION} (account ${ACCOUNT}): the Lambda function,"
echo "schedules, SNS topic and subscriptions, DynamoDB state table and log group."
((PURGE)) && echo "It also empties and deletes s3://${BUCKET}."
if ((!YES)); then
  read -r -p "Type the stack name to confirm: " answer
  [[ "$answer" == "$STACK" ]] || { echo "Aborted."; exit 1; }
fi

aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
echo "Waiting for the stack to be deleted..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
echo "Stack deleted."

if ((PURGE)); then
  if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
    aws s3 rm "s3://${BUCKET}" --recursive --region "$REGION"
    aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION"
    echo "Bucket deleted."
  fi
fi
