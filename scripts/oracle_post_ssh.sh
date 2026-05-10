#!/usr/bin/env bash
# ===========================================================================
# À exécuter UNE FOIS via SSH après que l'instance OCI a fini son
# cloud-init. Idempotent — relance safe.
#
# Usage (sur le serveur, après `ssh polymarket-prod`):
#   curl -fsSL https://raw.githubusercontent.com/<toi>/<repo>/main/scripts/oracle_post_ssh.sh | bash
#   # ou bien : scp ce fichier puis bash oracle_post_ssh.sh
#
# Ce que ça vérifie / installe :
#   - cloud-init terminé proprement
#   - docker + compose dispo, user ubuntu dans le groupe docker
#   - clone du repo (si pas déjà là)
#   - .env présent (sinon avorte avec un message clair)
#   - première stack-up (postgres + redis seuls, sans les apps)
# ===========================================================================
set -euo pipefail

REPO_URL="${POLYMARKET_REPO_URL:-}"
PROJECT_DIR="/opt/polymarket-bot"
LOG_PREFIX="[bootstrap]"

log() { echo "$LOG_PREFIX $*"; }
fail() { echo "$LOG_PREFIX ERROR: $*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 1. Sanity                                                                    #
# --------------------------------------------------------------------------- #
log "1/6 — checking cloud-init status"
if ! sudo cloud-init status --wait | grep -q "status: done"; then
    fail "cloud-init not finished — re-run me in 60s"
fi

if [[ ! -f /var/log/cloud-init-bot.log ]]; then
    fail "cloud-init didn't run our user-data — re-create the VM with the right yml"
fi

# --------------------------------------------------------------------------- #
# 2. Docker                                                                    #
# --------------------------------------------------------------------------- #
log "2/6 — verifying Docker"
docker --version || fail "docker not on PATH"
docker compose version || fail "docker compose plugin missing"

if ! id -nG ubuntu | grep -qw docker; then
    fail "user 'ubuntu' not in docker group — log out and back in"
fi

# --------------------------------------------------------------------------- #
# 3. Clone du repo                                                             #
# --------------------------------------------------------------------------- #
log "3/6 — fetching repo into $PROJECT_DIR"
if [[ -z "$REPO_URL" && ! -d "$PROJECT_DIR/.git" ]]; then
    fail "POLYMARKET_REPO_URL not set and no clone yet — export it (or scp the repo manually)"
fi

if [[ ! -d "$PROJECT_DIR/.git" ]]; then
    git clone "$REPO_URL" "$PROJECT_DIR"
else
    log "  repo already cloned — pulling latest"
    git -C "$PROJECT_DIR" pull --ff-only
fi
cd "$PROJECT_DIR"

# --------------------------------------------------------------------------- #
# 4. .env                                                                      #
# --------------------------------------------------------------------------- #
log "4/6 — checking .env"
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    cat <<'EOF'
[bootstrap] ERROR: .env not found.

Sur ta machine locale :
  scp polymarket-bot/.env polymarket-prod:/opt/polymarket-bot/.env

Puis relance ce script.

Rappel : .env doit contenir DATABASE_URL, REDIS_URL, et les clés
TELEGRAM_*, R2_*, etc. selon ce qui est activé en prod.
EOF
    exit 1
fi
chmod 600 "$PROJECT_DIR/.env"

# --------------------------------------------------------------------------- #
# 5. Build de l'image                                                          #
# --------------------------------------------------------------------------- #
log "5/6 — building the bot image (this takes ~3-5 min on Ampere A1)"
docker compose build

# --------------------------------------------------------------------------- #
# 6. Backend up (postgres + redis seulement)                                   #
# --------------------------------------------------------------------------- #
log "6/6 — starting backends (postgres + redis)"
docker compose up -d postgres redis

# Wait for postgres health.
for i in {1..30}; do
    state=$(docker inspect --format='{{.State.Health.Status}}' polymarket_db 2>/dev/null || echo "starting")
    if [[ "$state" == "healthy" ]]; then break; fi
    log "  waiting for postgres ($state) — $i/30"
    sleep 2
done
[[ "$state" == "healthy" ]] || fail "postgres did not become healthy in 60s"

log ""
log "✅ Bootstrap done. Next steps:"
log "   1. Run DB migrations:   docker compose run --rm engine alembic upgrade head"
log "   2. Smoke-test backups:  docker compose run --rm backups python scripts/backup_db.py --output /tmp/snap.dump"
log "   3. Bring up the apps:   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d"
log ""
