#!/usr/bin/env bash
# Ship this checkout to the EC2 instance Terraform created and start the stack.
#
#   cd infra && terraform init && terraform apply     # once
#   ./deploy.sh                                      # every deploy
#
# Idempotent: run it again after any code change and it rebuilds and restarts
# only what changed. Works from Git Bash on Windows as well as Linux/macOS.
set -euo pipefail

cd "$(dirname "$0")"

INFRA_DIR="infra"
REMOTE_DIR="/opt/app"

# Values a human chose that legitimately differ between local dev and this
# deployment (e.g. a separate Gemini key/quota) -- distinct from the
# generated secrets built below (DB password, Django key, Flower auth),
# which never touch a file you maintain. .env.prod is gitignored, like .env;
# copy .env.prod.example to get started. A real shell export always wins.
# There is deliberately no fallback to the local dev .env here.
env_prod_get() {
  if [ -f .env.prod ]; then
    sed -n "s/^$1=//p" .env.prod | head -1
  fi
}

LLM_PROVIDER="${LLM_PROVIDER:-$(env_prod_get LLM_PROVIDER)}"
LLM_PROVIDER="${LLM_PROVIDER:-gemini}"
GEMINI_KEY="${GEMINI_API_KEY:-$(env_prod_get GEMINI_API_KEY)}"
GEMINI_MODEL="${GEMINI_MODEL:-$(env_prod_get GEMINI_MODEL)}"
ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-$(env_prod_get ANTHROPIC_API_KEY)}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$(env_prod_get ANTHROPIC_MODEL)}"
OPENAI_KEY="${OPENAI_API_KEY:-$(env_prod_get OPENAI_API_KEY)}"
OPENAI_MODEL="${OPENAI_MODEL:-$(env_prod_get OPENAI_MODEL)}"
SEED_ROWS="${SEED_LARGE_ROWS:-$(env_prod_get SEED_LARGE_ROWS)}"
SEED_ROWS="${SEED_ROWS:-2000000}"

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

command -v terraform >/dev/null || die "terraform is not on PATH"
command -v ssh >/dev/null || die "ssh is not on PATH"

# ---------------------------------------------------------------------------- #
# 1. read the infrastructure Terraform just built
# ---------------------------------------------------------------------------- #
step "Reading Terraform outputs"
tf() { terraform -chdir="$INFRA_DIR" output -raw "$1" 2>/dev/null || true; }

HOST="$(tf public_ip)"
BUCKET="$(tf s3_bucket)"
REGION="$(tf region)"
CDN_DOMAIN="$(tf cdn_domain)"
INSTANCE_DNS="$(tf instance_dns)"
[ -n "$HOST" ] || die "no public_ip output -- run 'cd $INFRA_DIR && terraform apply' first"
[ -n "$BUCKET" ] || die "no s3_bucket output -- is the Terraform state complete?"

# SSH_KEY wins, so someone who supplied their own keypair to Terraform is not
# blocked by the generated-key check below.
PROJECT="$(tf project)"
KEY="${SSH_KEY:-$INFRA_DIR/${PROJECT:-regex-matching}-key.pem}"
[ -f "$KEY" ] || die "SSH key not found at $KEY (set SSH_KEY=/path/to/key if you supplied your own)"
chmod 600 "$KEY" 2>/dev/null || true

echo "host   : $HOST"
echo "bucket : $BUCKET ($REGION)"
[ -n "$CDN_DOMAIN" ] && echo "cdn    : https://$CDN_DOMAIN/"

# Django validates the Host header, so every name the app can be reached by has
# to be listed -- the IP, the AWS-assigned hostname, and the CloudFront domain.
# Miss one and that name serves the frontend fine but answers 400 on /api/,
# which looks like a broken API rather than a host check.
ALLOWED="$HOST,localhost,127.0.0.1,backend"
ORIGINS="http://$HOST"
[ -n "$INSTANCE_DNS" ] && ALLOWED="$ALLOWED,$INSTANCE_DNS" && ORIGINS="$ORIGINS,http://$INSTANCE_DNS"
if [ -n "$CDN_DOMAIN" ]; then
  ALLOWED="$ALLOWED,$CDN_DOMAIN"
  ORIGINS="$ORIGINS,https://$CDN_DOMAIN"
  PUBLIC_URL="https://$CDN_DOMAIN/"
else
  PUBLIC_URL="http://$HOST/"
fi

SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
remote() { ssh "${SSH_OPTS[@]}" "ubuntu@$HOST" "$@"; }

# ---------------------------------------------------------------------------- #
# 2. wait for the instance bootstrap to finish
# ---------------------------------------------------------------------------- #
step "Waiting for the instance to finish bootstrapping (Docker install)"
for attempt in $(seq 1 60); do
  if remote 'test -f /var/lib/cloud-bootstrap-complete' 2>/dev/null; then
    echo "ready"
    break
  fi
  [ "$attempt" -eq 60 ] && die "instance never finished bootstrapping; check /var/log/user-data.log on the box"
  printf '.'
  sleep 10
done

# ---------------------------------------------------------------------------- #
# 3. build the production .env
#
# Secrets are generated here and kept only on the instance. Note what is absent:
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY. The instance profile supplies S3
# credentials, so there are no long-lived keys on the box at all.
# ---------------------------------------------------------------------------- #
step "Generating production .env"

# No fallback to the local dev .env on purpose: a prod deploy silently
# reusing your dev key/quota is a bug, not a convenience. Fail loudly instead.
case "$LLM_PROVIDER" in
  gemini)
    [ -n "$GEMINI_KEY" ] || die "no Gemini key: put GEMINI_API_KEY=... in .env.prod (copy .env.prod.example) or export GEMINI_API_KEY=..."
    ;;
  anthropic|claude)
    [ -n "$ANTHROPIC_KEY" ] || die "no Anthropic key: put ANTHROPIC_API_KEY=... in .env.prod (copy .env.prod.example) or export ANTHROPIC_API_KEY=..."
    ;;
  openai|gpt|chatgpt)
    [ -n "$OPENAI_KEY" ] || die "no OpenAI key: put OPENAI_API_KEY=... in .env.prod (copy .env.prod.example) or export OPENAI_API_KEY=..."
    ;;
  *)
    die "unknown LLM_PROVIDER: $LLM_PROVIDER (expected gemini, anthropic, or openai)"
    ;;
esac

rand() {
  local raw
  raw="$(LC_ALL=C tr -dc 'A-Za-z0-9' < <(head -c "$(( $1 * 16 ))" /dev/urandom))"
  printf '%s' "${raw:0:$1}"
}

# Reuse the previous run's secrets so a redeploy does not invalidate the Postgres
# password against an existing data volume.
if remote "test -f $REMOTE_DIR/.env" 2>/dev/null; then
  echo "reusing existing secrets from the instance"
  DB_PASSWORD="$(remote "sed -n 's/^POSTGRES_PASSWORD=//p' $REMOTE_DIR/.env | head -1")"
  SECRET_KEY="$(remote "sed -n 's/^DJANGO_SECRET_KEY=//p' $REMOTE_DIR/.env | head -1")"
  FLOWER_AUTH="$(remote "sed -n 's/^FLOWER_BASIC_AUTH=//p' $REMOTE_DIR/.env | head -1")"
fi
DB_PASSWORD="${DB_PASSWORD:-$(rand 24)}"
SECRET_KEY="${SECRET_KEY:-$(rand 50)}"
FLOWER_AUTH="${FLOWER_AUTH:-admin:$(rand 16)}"

ENV_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE"' EXIT

cat > "$ENV_FILE" <<ENVEOF
# Generated by deploy.sh -- do not edit by hand, it is overwritten every deploy.
DJANGO_SETTINGS_MODULE=config.settings.prod
DJANGO_SECRET_KEY=$SECRET_KEY
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=$ALLOWED
CORS_ALLOWED_ORIGINS=$ORIGINS

POSTGRES_DB=regexdb
POSTGRES_USER=regex
POSTGRES_PASSWORD=$DB_PASSWORD
POSTGRES_HOST=postgres
POSTGRES_PORT=5432

REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2

# Real Amazon S3. No endpoint override, virtual-host addressing, TLS on, and
# NO access keys -- the EC2 instance profile provides credentials.
S3_ENDPOINT_URL=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_DEFAULT_REGION=$REGION
S3_BUCKET=$BUCKET
S3_RAW_PREFIX=raw/
S3_RESULT_PREFIX=results/
S3_USE_SSL=1
S3_PATH_STYLE=0

SPARK_MASTER_URL=spark://spark-master:7077
SPARK_DRIVER_HOST=celery-worker
SPARK_DRIVER_MEMORY=1g
SPARK_EXECUTOR_MEMORY=1500m
SPARK_EXECUTOR_CORES=2
SPARK_SHUFFLE_PARTITIONS=8
SPARK_MAX_PARTITION_BYTES=64m
SPARK_WORKER_CORES=2
SPARK_WORKER_MEMORY=2g
SPARK_MAX_RECORDS_PER_FILE=100000
SPARK_LOG_LEVEL=WARN
EXCEL_MAX_BYTES=104857600

LLM_PROVIDER=$LLM_PROVIDER
GEMINI_API_KEY=$GEMINI_KEY
GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.6-flash}
GEMINI_MAX_OUTPUT_TOKENS=8192
ANTHROPIC_API_KEY=$ANTHROPIC_KEY
ANTHROPIC_MODEL=${ANTHROPIC_MODEL:-claude-opus-5}
ANTHROPIC_MAX_OUTPUT_TOKENS=8192
OPENAI_API_KEY=$OPENAI_KEY
OPENAI_MODEL=${OPENAI_MODEL:-gpt-4.1}
OPENAI_MAX_OUTPUT_TOKENS=8192
LLM_CACHE_TTL_SECONDS=604800
LLM_TIMEOUT_SECONDS=30

REGEX_MAX_LENGTH=2000
REGEX_TIMEOUT_MS=1000
API_MAX_PAGE_SIZE=500

FLOWER_BASIC_AUTH=$FLOWER_AUTH
SEED_LARGE_ROWS=$SEED_ROWS
SEED_FORCE=0
LOG_FORMAT=json
ENVEOF

# ---------------------------------------------------------------------------- #
# 4. copy the source
# ---------------------------------------------------------------------------- #
step "Copying the application to $HOST:$REMOTE_DIR"
TARBALL="$(mktemp -t app-XXXXXX.tar.gz)"
trap 'rm -f "$ENV_FILE" "$TARBALL"' EXIT

# Patterns without a slash match any path component, so these catch nested
# directories without needing globs that tar would not expand.
tar czf "$TARBALL" \
  --exclude='node_modules' \
  --exclude='dist' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='*.pyc' \
  ./backend ./frontend ./seed ./docker-compose.prod.yml ./README.md

remote "mkdir -p $REMOTE_DIR"
scp "${SSH_OPTS[@]}" -q "$TARBALL" "ubuntu@$HOST:$REMOTE_DIR/app.tar.gz"
scp "${SSH_OPTS[@]}" -q "$ENV_FILE" "ubuntu@$HOST:$REMOTE_DIR/.env"
remote "cd $REMOTE_DIR && tar xzf app.tar.gz && rm app.tar.gz && chmod 600 .env"

# ---------------------------------------------------------------------------- #
# 5. build and start
# ---------------------------------------------------------------------------- #
step "Building images on the instance (first run takes several minutes)"
remote "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml build"

step "Starting the stack"
remote "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml up -d --remove-orphans"

step "Seeding the S3 bucket (skips objects that already exist)"
remote "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml run --rm seed"

# ---------------------------------------------------------------------------- #
# 6. verify
# ---------------------------------------------------------------------------- #
step "Waiting for the API to report healthy"
HEALTHY=0
for attempt in $(seq 1 40); do
  STATUS="$(curl -fsS --max-time 10 "http://$HOST/api/health/" 2>/dev/null | head -c 400 || true)"
  case "$STATUS" in
    *'"status":"ok"'*) HEALTHY=1; break ;;
  esac
  printf '.'
  sleep 10
done
echo

if [ "$HEALTHY" -ne 1 ]; then
  echo "The API is not healthy yet. Last response:"
  echo "  ${STATUS:-<no response>}"
  echo
  echo "Inspect it with:"
  echo "  ssh -i $KEY ubuntu@$HOST 'cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml ps'"
  echo "  ssh -i $KEY ubuntu@$HOST 'cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml logs --tail=80 backend'"
  exit 1
fi

cat <<SUMMARY

  Deployed.

  App     $PUBLIC_URL              <-- send this one
  Health  ${PUBLIC_URL}api/health/
  Origin  http://$HOST/            (the instance itself, bypassing the CDN)
  Flower  ${PUBLIC_URL}flower/     ($FLOWER_AUTH)
          http://$HOST:5555/flower/   (direct, bypassing the CDN)
  Bucket  s3://$BUCKET

  Spark UIs are bound to localhost on the instance; tunnel them with
    ssh -i $KEY -L 8080:localhost:8080 -L 4040:localhost:4040 ubuntu@$HOST

SUMMARY
