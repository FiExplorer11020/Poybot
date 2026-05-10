#!/usr/bin/env bash
# ===========================================================================
# À exécuter UNE FOIS via SSH après que cloud-init a fini sur la VM Hetzner.
# Idempotent — relance safe.
#
# Usage (sur le serveur, après `ssh polymarket-prod`):
#   bash /opt/polymarket-bot/scripts/hetzner_post_ssh.sh
#
# Pré-requis (faits par cloud-init ou par toi avant) :
#   - cloud-init terminé (/var/log/cloud-init-bot.log existe)
#   - repo cloné dans /opt/polymarket-bot (ou scp'é)
#   - .env présent dans /opt/polymarket-bot/.env
#   - dump SQL présent dans /tmp/polymarket_dump.sql.gz (optionnel — pour
#     restore depuis ton local). Si absent, le script monte une DB vide
#     et applique les migrations.
# ===========================================================================
set -euo pipefail

PROJECT_DIR="/opt/polymarket-bot"
LOG_PREFIX="[bootstrap]"

log() { echo "$LOG_PREFIX $*"; }
fail() { echo "$LOG_PREFIX ERROR: $*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 1. Sanity                                                                    #
# --------------------------------------------------------------------------- #
log "1/7 — checking cloud-init"
if [[ ! -f /var/log/cloud-init-bot.log ]]; then
    fail "cloud-init not finished — wait a bit more or check /var/log/cloud-init-output.log"
fi
log "  cloud-init done:"
sed 's/^/    /' /var/log/cloud-init-bot.log

# --------------------------------------------------------------------------- #
# 2. Docker check                                                              #
# --------------------------------------------------------------------------- #
log "2/7 — verifying Docker"
docker --version || fail "docker not on PATH"
docker compose version || fail "docker compose plugin missing"
if ! id -nG "$(whoami)" | grep -qw docker; then
    fail "user $(whoami) not in docker group — log out and back in (or run as polymarket)"
fi

# --------------------------------------------------------------------------- #
# 3. Project files                                                             #
# --------------------------------------------------------------------------- #
log "3/7 — checking project layout"
[[ -d "$PROJECT_DIR" ]] || fail "$PROJECT_DIR doesn't exist — scp the repo first"
cd "$PROJECT_DIR"

if [[ ! -f .env ]]; then
    cat <<EOF
$LOG_PREFIX ERROR: .env not found.

Sur ta machine locale :
  scp /Users/oscargrima/Documents/Claude/Projects/Polymarket\\ trading\\ bot/polymarket-bot/.env \\
      polymarket-prod:/opt/polymarket-bot/.env

Puis relance ce script.

Le .env doit contenir au minimum :
  DATABASE_URL    (sera réécrit côté compose en interne)
  REDIS_URL       (idem)
  FALCON_API_KEY  (clé Falcon)
  TELEGRAM_*      (si bot Telegram activé)
  R2_*            (si backups R2 activés)
EOF
    exit 1
fi
chmod 600 .env

# --------------------------------------------------------------------------- #
# 4. Build de l'image                                                          #
# --------------------------------------------------------------------------- #
log "4/7 — building polymarket-bot:latest (3-5 min)"
docker compose build

# --------------------------------------------------------------------------- #
# 5. Backends up (postgres + redis)                                            #
# --------------------------------------------------------------------------- #
log "5/7 — starting postgres + redis"
docker compose up -d postgres redis

# Wait for postgres health.
log "  waiting for postgres healthy..."
for i in {1..30}; do
    state=$(docker inspect --format='{{.State.Health.Status}}' polymarket_db 2>/dev/null || echo "starting")
    if [[ "$state" == "healthy" ]]; then break; fi
    sleep 2
done
[[ "$state" == "healthy" ]] || fail "postgres did not become healthy in 60s"
log "  postgres healthy ✅"

# --------------------------------------------------------------------------- #
# 6. Restore DB (optionnel — si dump présent)                                  #
# --------------------------------------------------------------------------- #
log "6/7 — checking for DB dump"
if [[ -f /tmp/polymarket_dump.sql.gz ]]; then
    log "  found /tmp/polymarket_dump.sql.gz, restoring..."
    gunzip -c /tmp/polymarket_dump.sql.gz | docker exec -i polymarket_db psql -U polymarket -d polymarket
    log "  restore done. Counts :"
    docker exec polymarket_db psql -U polymarket -d polymarket -c "
      SELECT 'leaders' as table, count(*) FROM leaders UNION ALL
      SELECT 'trades_observed', count(*) FROM trades_observed UNION ALL
      SELECT 'positions_reconstructed', count(*) FROM positions_reconstructed UNION ALL
      SELECT 'follower_edges', count(*) FROM follower_edges UNION ALL
      SELECT 'leader_profiles', count(*) FROM leader_profiles UNION ALL
      SELECT 'paper_trades', count(*) FROM paper_trades UNION ALL
      SELECT 'decision_log', count(*) FROM decision_log;
    "
elif [[ -f /tmp/polymarket_dump.dump ]]; then
    log "  found /tmp/polymarket_dump.dump (custom format), restoring with pg_restore..."
    docker exec -i polymarket_db pg_restore -U polymarket -d polymarket --clean --if-exists --no-owner < /tmp/polymarket_dump.dump
else
    log "  no dump found — running setup_db.py for fresh schema"
    docker compose run --rm engine python scripts/setup_db.py
fi

# --------------------------------------------------------------------------- #
# 7. Done                                                                       #
# --------------------------------------------------------------------------- #
log ""
log "✅ Bootstrap done. Next steps:"
log "   1. Bring up the apps:"
log "      docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d"
log ""
log "   2. Tail logs to verify:"
log "      docker compose logs -f --tail=50 engine observer"
log ""
log "   3. Smoke test the API (from your laptop, replace IP):"
log "      curl http://<HETZNER_IP>:8080/healthz"
log ""
log "   4. Manuel backup test (after BACKUPS_ENABLED=true in .env):"
log "      docker compose run --rm backups python scripts/backup_db.py --output /tmp/snap.dump"
log ""
