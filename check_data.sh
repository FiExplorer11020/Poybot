#!/usr/bin/env bash
# Audit de l'état actuel de la donnée locale.
# Sortie: data_audit.txt (lisible par Claude)

set +e
OUT="data_audit.txt"
cd "$(dirname "$0")"

echo "=== $(date) ===" > "$OUT"

echo -e "\n--- 1. Conteneurs Docker (running + stopped) ---" >> "$OUT"
docker compose ps -a >> "$OUT" 2>&1

echo -e "\n--- 2. Volumes Postgres/Redis ---" >> "$OUT"
docker volume ls | grep -E "polymarket|postgres|redis" >> "$OUT" 2>&1

echo -e "\n--- 3. Tables présentes ---" >> "$OUT"
docker compose exec -T postgres psql -U polymarket -d polymarket -c "\dt" >> "$OUT" 2>&1

echo -e "\n--- 4. Lignes par table (volumétrie) ---" >> "$OUT"
docker compose exec -T postgres psql -U polymarket -d polymarket -c \
  "SELECT relname AS table, n_live_tup AS rows, pg_size_pretty(pg_total_relation_size(relid)) AS size FROM pg_stat_user_tables ORDER BY n_live_tup DESC;" >> "$OUT" 2>&1

echo -e "\n--- 5. Plage temporelle des données (si tables observer présentes) ---" >> "$OUT"
docker compose exec -T postgres psql -U polymarket -d polymarket -c \
  "SELECT 'trades' AS tbl, MIN(created_at) AS first, MAX(created_at) AS last, COUNT(*) FROM trades;" >> "$OUT" 2>&1

echo -e "\n--- 6. Taille totale DB ---" >> "$OUT"
docker compose exec -T postgres psql -U polymarket -d polymarket -c \
  "SELECT pg_size_pretty(pg_database_size('polymarket'));" >> "$OUT" 2>&1

echo -e "\n=== fin ===" >> "$OUT"
echo "Resultat ecrit dans $(pwd)/$OUT"
