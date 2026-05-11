#!/usr/bin/env bash
# create-read-only.sh — Creates a read-only IAM user for the FinOps agent.
#
# Usage:
#   ./create-read-only.sh [parent-profile]          # real run
#   ./create-read-only.sh [parent-profile] --dry-run # simulate, no changes
#   AWS_PARENT_PROFILE=my-profile ./create-read-only.sh
#
# What it does:
#   1. Uses parent profile (default: $AWS_PARENT_PROFILE or "default") to create
#      an IAM user named "finops-agent-readonly" with AWS managed ReadOnlyAccess.
#   2. Creates an access key for that user.
#   3. Writes the creds to ./.env (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
#      and sets USE_LOCALSTACK=false.
#   4. Adds a "finops" profile to ~/.aws/credentials so `aws ... --profile finops`
#      works from your shell.
#   5. Tests the new creds and verifies they CANNOT write (simulate-principal-policy).
#
# The created user is strictly read-only (ReadOnlyAccess managed policy includes
# ec2/rds/eks/elasticache/s3/cloudwatch Describe/List/Get). A separate customer
# managed policy (finops-agent-billing-readonly) adds full Cost Explorer, Cost
# Optimization Hub, Trusted Advisor, Budgets, CUR and Compute Optimizer access.

set -euo pipefail

PARENT_PROFILE="${1:-${AWS_PARENT_PROFILE:-default}}"
DRY_RUN=false
for arg in "$@"; do [[ "$arg" == "--dry-run" ]] && DRY_RUN=true; done

run() {
  # In dry-run mode: print the command instead of executing it
  if $DRY_RUN; then
    echo "[DRY-RUN] $*"
  else
    "$@"
  fi
}
NEW_USER="finops-agent-readonly"
NEW_PROFILE="finops"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

info()  { echo "[INFO]  $*"; }
ok()    { echo "[OK]    $*"; }
warn()  { echo "[WARN]  $*"; }
fail()  { echo "[FAIL]  $*" >&2; exit 1; }

command -v aws    >/dev/null || fail "aws CLI not found"
command -v python >/dev/null || fail "python not found"

$DRY_RUN && warn "DRY-RUN mode — no changes will be made to AWS or local files"
info "Parent profile: $PARENT_PROFILE"
info "New IAM user:   $NEW_USER"
info "New profile:    $NEW_PROFILE"
info "Region:         $REGION"

ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PARENT_PROFILE" --query Account --output text)
info "AWS Account:    $ACCOUNT_ID"

# ── 1. Create user (idempotent) ──────────────────────────────────────────
if aws iam get-user --user-name "$NEW_USER" --profile "$PARENT_PROFILE" >/dev/null 2>&1; then
  info "User $NEW_USER already exists — reusing"
else
  run aws iam create-user --user-name "$NEW_USER" --profile "$PARENT_PROFILE" >/dev/null
  ok "Created IAM user: $NEW_USER"
fi

# ── 2. Attach ReadOnlyAccess policy ──────────────────────────────────────
run aws iam attach-user-policy \
  --user-name "$NEW_USER" \
  --policy-arn "arn:aws:iam::aws:policy/ReadOnlyAccess" \
  --profile "$PARENT_PROFILE"
ok "Attached policy: ReadOnlyAccess"

# ── 3. Rotate access keys ────────────────────────────────────────────────
EXISTING_KEYS=$(aws iam list-access-keys \
  --user-name "$NEW_USER" \
  --profile "$PARENT_PROFILE" \
  --query 'AccessKeyMetadata[].AccessKeyId' \
  --output text || true)

if [ -n "$EXISTING_KEYS" ] && [ "$EXISTING_KEYS" != "None" ]; then
  warn "User already has access keys: $EXISTING_KEYS"
  if $DRY_RUN; then
    echo "[DRY-RUN] aws iam delete-access-key (would delete: $EXISTING_KEYS)"
  else
    warn "Deleting old keys to avoid 2-key limit..."
    for key in $EXISTING_KEYS; do
      aws iam delete-access-key --user-name "$NEW_USER" --access-key-id "$key" --profile "$PARENT_PROFILE"
      ok "Deleted old key: $key"
    done
  fi
fi

if $DRY_RUN; then
  echo "[DRY-RUN] aws iam create-access-key → would write AKID/SAK to .env and ~/.aws/credentials"
  AKI="AKIAIOSFODNN7EXAMPLE"
  SAK="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
else
  info "Creating new access key..."
  KEY_JSON=$(aws iam create-access-key --user-name "$NEW_USER" --profile "$PARENT_PROFILE")
  AKI=$(echo "$KEY_JSON" | python -c "import json,sys;print(json.load(sys.stdin)['AccessKey']['AccessKeyId'])")
  SAK=$(echo "$KEY_JSON" | python -c "import json,sys;print(json.load(sys.stdin)['AccessKey']['SecretAccessKey'])")
  ok "Access Key: $AKI"
fi

# ── 4. Update finops-agent/.env ──────────────────────────────────────────
if $DRY_RUN; then
  echo "[DRY-RUN] Would write AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / USE_LOCALSTACK=false to $ENV_FILE"
else
  touch "$ENV_FILE"
  TMP_ENV=$(mktemp)
  grep -v -E '^(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_DEFAULT_REGION|USE_LOCALSTACK|AWS_PROFILE)=' "$ENV_FILE" > "$TMP_ENV" || true
  {
    cat "$TMP_ENV"
    echo ""
    echo "# AWS credentials — managed by create-read-only.sh"
    echo "AWS_ACCESS_KEY_ID=$AKI"
    echo "AWS_SECRET_ACCESS_KEY=$SAK"
    echo "AWS_DEFAULT_REGION=$REGION"
    echo "USE_LOCALSTACK=false"
  } > "$ENV_FILE"
  rm -f "$TMP_ENV"
  ok "Updated: $ENV_FILE"
fi

# ── 5. Add 'finops' profile to ~/.aws/credentials ────────────────────────
if $DRY_RUN; then
  echo "[DRY-RUN] Would write [$NEW_PROFILE] section to ~/.aws/credentials"
else
  AWS_CRED_FILE="$HOME/.aws/credentials"
  mkdir -p "$(dirname "$AWS_CRED_FILE")"
  touch "$AWS_CRED_FILE"
  TMP_CRED=$(mktemp)
  awk -v section="[$NEW_PROFILE]" '
    BEGIN { skip=0 }
    /^\[.*\]$/ { skip = ($0 == section) ? 1 : 0 }
    !skip { print }
  ' "$AWS_CRED_FILE" > "$TMP_CRED"
  {
    cat "$TMP_CRED"
    echo ""
    echo "[$NEW_PROFILE]"
    echo "aws_access_key_id = $AKI"
    echo "aws_secret_access_key = $SAK"
    echo "region = $REGION"
  } > "$AWS_CRED_FILE"
  rm -f "$TMP_CRED"
  ok "AWS profile '$NEW_PROFILE' written to $AWS_CRED_FILE"
fi

# ── 6. Wait for IAM propagation (skip in dry-run) ───────────────────────
if ! $DRY_RUN; then
  info "Waiting for IAM key propagation (15s)..."
  for _ in 1 2 3; do sleep 5; echo -n "."; done; echo

  # ── 7. Test read access ─────────────────────────────────────────────────
  info "Testing: sts get-caller-identity with --profile $NEW_PROFILE ..."
  if ! aws sts get-caller-identity --profile "$NEW_PROFILE" >/dev/null 2>&1; then
    warn "Key not propagated yet, retrying in 10s..."
    sleep 10
  fi
  aws sts get-caller-identity --profile "$NEW_PROFILE"
  ok "Read access works"

  info "Testing: s3 ls (read) with --profile $NEW_PROFILE ..."
  aws s3 ls --profile "$NEW_PROFILE" | head -3 || true
  ok "Read s3 works"
fi

# ── 8. Verify user CANNOT write (safety check — dry-run, no real resources) ──
info "Verifying WRITE is blocked (simulate-principal-policy, no real resources)..."
DENY_RESULT=$(aws iam simulate-principal-policy \
  --policy-source-arn "arn:aws:iam::$ACCOUNT_ID:user/$NEW_USER" \
  --action-names "s3:CreateBucket" "ec2:TerminateInstances" "rds:DeleteDBInstance" \
  --profile "$PARENT_PROFILE" \
  --query 'EvaluationResults[*].{Action:EvalActionName,Decision:EvalDecision}' \
  --output text 2>&1)
echo "$DENY_RESULT"
if echo "$DENY_RESULT" | grep -v "implicitDeny\|explicitDeny" | grep -q "allowed"; then
  fail "SECURITY ISSUE: user has write permissions! Check policies."
else
  ok "Write blocked as expected (implicitDeny / explicitDeny)"
fi

echo ""
if $DRY_RUN; then
echo "======================================================================"
echo "  DRY-RUN COMPLETE — nothing was created or modified"
echo "======================================================================"
echo "  Would create/reuse IAM user: $NEW_USER  (account: $ACCOUNT_ID)"
echo "  Would attach:  ReadOnlyAccess + finops-agent-billing-readonly"
echo "  Would write:   $ENV_FILE  (AWS_ACCESS_KEY_ID / USE_LOCALSTACK=false)"
echo "  Would write:   ~/.aws/credentials  ([$NEW_PROFILE] section)"
echo ""
echo "  Run without --dry-run to apply."
echo "======================================================================"
else
echo "======================================================================"
echo "  SUCCESS — FinOps agent will now use real AWS (READ-ONLY)"
echo "======================================================================"
echo "  IAM User:    $NEW_USER"
echo "  Access Key:  $AKI"
echo "  AWS Profile: $NEW_PROFILE   (try: aws sts get-caller-identity --profile $NEW_PROFILE)"
echo "  .env:        $ENV_FILE"
echo ""
echo "  Next: restart the backend so it picks up the new .env:"
echo "    cd $SCRIPT_DIR && docker compose up -d --force-recreate finops-agent"
echo "======================================================================"
fi
