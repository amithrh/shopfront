#!/usr/bin/env bash
# Build, push and deploy AskOnce to ap-southeast-2. Idempotent: safe to re-run after any change.
#
#   AWS_PROFILE=shopfront infra/deploy.sh            # from anywhere; needs docker, uv, node/npx, aws CLI
#
# Phases (each skipped when already satisfied):
#   1. CDK deploy of the core layer (tables, secrets, ECR, logs) if the stack has no ECR repository yet.
#   2. Build the arm64 image (tag = git short sha, plus -dirty-<time> for uncommitted trees) and push it
#      unless ECR already has that tag (tags are immutable).
#   3. CDK deploy with the service (context image_tag, public_host = the service's existing endpoint).
#   4. On the first service deploy the endpoint is unknown, so the container starts with the placeholder
#      host (pending.invalid: /health is green, /mcp answers 421); the script reads the ServiceEndpoint
#      output and deploys once more with the real host.
# Never bootstraps: run `cdk bootstrap` once yourself (see infra/README.md).
set -euo pipefail

REGION=ap-southeast-2
STACK=AskOnceCore
CDK_VERSION=2.1143.0
export AWS_PROFILE="${AWS_PROFILE:-shopfront}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" CDK_DEPLOY_REGION="$REGION"
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1
APPROVAL="${CDK_APPROVAL:-broadening}"   # set CDK_APPROVAL=never for unattended runs

INFRA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$INFRA/.." && pwd)"
cd "$INFRA"

cdk() { npx -y "aws-cdk@${CDK_VERSION}" "$@"; }
output() {  # output <key>: a stack output, or empty when the stack/output does not exist
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text 2>/dev/null \
    | sed 's/^None$//'
}
strip_host() { sed -E 's#^https?://##; s#/.*$##' <<<"$1"; }

uv sync --frozen --quiet
export CDK_DEFAULT_ACCOUNT CDK_DEFAULT_REGION="$REGION"
CDK_DEFAULT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
echo "account $CDK_DEFAULT_ACCOUNT, region $REGION, profile $AWS_PROFILE"

# 1. Core layer.
REPO_URI="$(output RepositoryUri)"
if [[ -z "$REPO_URI" ]]; then
  echo "== deploying the core layer (no service yet)"
  cdk deploy "$STACK" --require-approval "$APPROVAL"
  REPO_URI="$(output RepositoryUri)"
fi

# 2. Image.
TAG="$(git -C "$REPO" rev-parse --short=12 HEAD)"
if [[ -n "$(git -C "$REPO" status --porcelain -- server packages/mcp-tasks schema profiles scripts/dev_seed_live.py)" ]]; then
  TAG="${TAG}-dirty-$(date -u +%Y%m%d%H%M%S)"
fi
if aws ecr describe-images --repository-name askonce-mcp --image-ids "imageTag=$TAG" --region "$REGION" >/dev/null 2>&1; then
  echo "== image $TAG already in ECR"
else
  echo "== building and pushing $REPO_URI:$TAG"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO_URI%%/*}"
  docker buildx build --platform linux/arm64 -f "$REPO/server/Dockerfile" -t "$REPO_URI:$TAG" --push "$REPO"
fi

# 3. Service (keeps the host it already has).
HOST="$(strip_host "$(output ServiceEndpoint)")"
echo "== deploying the service (image $TAG, public host ${HOST:-<placeholder>})"
cdk deploy "$STACK" --require-approval "$APPROVAL" -c image_tag="$TAG" ${HOST:+-c public_host="$HOST"}

# 4. First deploy: tell the server its real host.
ENDPOINT="$(strip_host "$(output ServiceEndpoint)")"
if [[ -n "$ENDPOINT" && "$ENDPOINT" != "$HOST" ]]; then
  echo "== service endpoint is $ENDPOINT; redeploying with it as the public host"
  cdk deploy "$STACK" --require-approval "$APPROVAL" -c image_tag="$TAG" -c public_host="$ENDPOINT"
fi

echo "== health"
curl -fsS --retry 6 --retry-delay 10 --retry-all-errors "https://${ENDPOINT}/health" && echo
echo "MCP endpoint: https://${ENDPOINT}/mcp"
echo "Seed the dev profiles (once, from $REPO): AWS_PROFILE=$AWS_PROFILE uv run --project server --with \"botocore[crt]==1.43.111\" python scripts/seed_dynamodb.py"
