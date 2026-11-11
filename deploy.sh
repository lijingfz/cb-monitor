#!/usr/bin/env bash
# One-command deployment of cb-monitor with the AWS CLI.
#
#   ./deploy.sh -e you@example.com                 # first deployment
#   ./deploy.sh                                     # update code, keep parameters
#   ./deploy.sh -p MinStartOffsetDays=3 -p ApiCallsPerMinute=4
#
# Options:
#   -e EMAIL        notification email (creates an SNS email subscription)
#   -r REGION       Region to deploy into (default: AWS_REGION, then the CLI default, then us-west-2)
#   -s STACK        stack name (default: cb-monitor)
#   -p KEY=VALUE    any template parameter, repeatable
#   -h              help
set -euo pipefail

cd "$(dirname "$0")"

STACK="cb-monitor"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
EMAIL=""
PARAMS=()

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

while getopts "e:r:s:p:h" opt; do
  case "$opt" in
    e) EMAIL="$OPTARG" ;;
    r) REGION="$OPTARG" ;;
    s) STACK="$OPTARG" ;;
    p) PARAMS+=("$OPTARG") ;;
    h) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done

command -v aws >/dev/null || { echo "AWS CLI v2 is required: https://aws.amazon.com/cli/" >&2; exit 1; }
if [[ -z "$REGION" ]]; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi
REGION="${REGION:-us-west-2}"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${STACK}-artifacts-${ACCOUNT}-${REGION}"
echo "Account: ${ACCOUNT}  Region: ${REGION}  Stack: ${STACK}"

# 1. Artifact bucket for the Lambda package (private, encrypted, old packages expire).
if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
  echo "Creating artifact bucket s3://${BUCKET}"
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-encryption --bucket "$BUCKET" --region "$REGION" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" \
    --lifecycle-configuration '{"Rules":[{"ID":"expire-old-packages","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":30}}]}'  >/dev/null
fi

# 2. Build: copy the code without caches, then package it.
BUILD=".build"
rm -rf "$BUILD"
mkdir -p "$BUILD"
cp template.yaml "$BUILD/"
cp -R src "$BUILD/src"
find "$BUILD/src" -name '__pycache__' -type d -prune -exec rm -rf {} +

aws cloudformation package \
  --region "$REGION" \
  --template-file "$BUILD/template.yaml" \
  --s3-bucket "$BUCKET" \
  --s3-prefix "$STACK" \
  --output-template-file "$BUILD/packaged.yaml" >/dev/null

# 3. Deploy. Parameters not given keep their current values on updates.
[[ -n "$EMAIL" ]] && PARAMS+=("NotificationEmail=${EMAIL}")
DEPLOY_ARGS=(
  --region "$REGION"
  --stack-name "$STACK"
  --template-file "$BUILD/packaged.yaml"
  --capabilities CAPABILITY_IAM
  --no-fail-on-empty-changeset
  --tags Project=cb-monitor
)
if ((${#PARAMS[@]})); then
  DEPLOY_ARGS+=(--parameter-overrides "${PARAMS[@]}")
fi
aws cloudformation deploy "${DEPLOY_ARGS[@]}"

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table

cat <<EOF

Deployed. Next steps:
  * If you passed -e, open the "AWS Notification - Subscription Confirmation" email and confirm.
  * Send a test email:   ./scripts/invoke.sh -r ${REGION} -s ${STACK} test
  * Run a scan now:      ./scripts/invoke.sh -r ${REGION} -s ${STACK} scan
  * Watch the logs:      aws logs tail /aws/lambda/${STACK}-monitor --region ${REGION} --follow
EOF
