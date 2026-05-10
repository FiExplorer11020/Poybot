# Deploy Manual — Mac → Hetzner Helsinki

Single source of truth pour pousser des modifs locales vers la VM de prod.
Tout autre doc qui mentionne le déploiement DOIT pointer vers ce fichier.

> **Production VM** : `polymarket@89.167.23.215` (Hetzner CX23, Helsinki HEL1)
> **Path on VM** : `/opt/polymarket-bot/`
> **SSH key** : `~/.ssh/hetzner_polymarket`
>
> ⚠️ Le path `/opt/polymarket-bot/` n'est PAS un repo git. Le déploiement
> est un `rsync` Mac → VM, pas un `git pull`. Les modifs Python/JSX/SQL
> doivent passer par cette procédure pour atteindre la prod.

---

## TL;DR — la procédure courte

```bash
# 1. Mac — commit + push (pour l'historique git)
cd "/Users/oscargrima/Documents/Claude/Projects/Polymarket trading bot/polymarket-bot"
git add -A
git commit -m "<message clair décrivant le changement>"
git push

# 2. Mac — sync les fichiers vers la VM
rsync -avz --delete \
  --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.pytest_cache/' --exclude '.ruff_cache/' --exclude '.mypy_cache/' \
  --exclude '.venv/' --exclude 'venv/' --exclude '*.egg-info/' \
  --exclude 'data_cache/' --exclude 'reports/' \
  --exclude '.DS_Store' --exclude '.claude/' \
  --exclude '*.log' --exclude 'orchestrate.log' \
  --exclude '.env' --exclude '.env.local' --exclude '.env.*.local' \
  -e "ssh -i ~/.ssh/hetzner_polymarket" \
  ./ polymarket@89.167.23.215:/opt/polymarket-bot/

# 3. VM — rebuild + restart (seulement si du code Python/Dockerfile a bougé)
ssh -i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215
cd /opt/polymarket-bot
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate

# 4. VM — vérifications post-deploy
docker ps --format "table {{.Names}}\t{{.Status}}"
curl -s http://localhost:8080/healthz
```

---

## Configuration SSH (à faire UNE FOIS)

Pour ne pas répéter `-i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215`
à chaque commande :

```bash
# Mac — édite ~/.ssh/config
cat >> ~/.ssh/config <<'EOF'

Host hetzner-polymarket
  HostName 89.167.23.215
  User polymarket
  IdentityFile ~/.ssh/hetzner_polymarket
  ServerAliveInterval 60
EOF
chmod 600 ~/.ssh/config
```

Ensuite `ssh hetzner-polymarket` te connecte direct, et le rsync devient
`-e ssh hetzner-polymarket:/opt/...`.

---

## Quand faut-il rebuild Docker ?

| Type de modif                                  | Rebuild ? | Restart ? |
|------------------------------------------------|-----------|-----------|
| Code Python (`src/**/*.py`, `scripts/*.py`)    | ✅ Oui    | ✅ Oui    |
| `Dockerfile`, `docker-compose*.yml`            | ✅ Oui    | ✅ Oui    |
| `pyproject.toml` (deps Python)                 | ✅ Oui    | ✅ Oui    |
| `.dockerignore`                                | ✅ Oui    | ✅ Oui    |
| Frontend JSX (`static/dashboard/*.jsx`)        | ❌ Non    | ❌ Non — hard refresh navigateur suffit |
| `templates/dashboard.html`                     | ❌ Non    | ❌ Non — hard refresh suffit |
| `docs/**/*.md`                                 | ❌ Non    | ❌ Non    |
| `docs/migrations/*.sql` (nouvelle migration)   | ✅ Oui    | ✅ Oui — la migration s'applique au boot |
| `scripts/*.sql` (one-shot)                     | ❌ Non    | ❌ Non — exécute via `psql` directement |

Le frontend JSX est servi en static par FastAPI et transformé par Babel-on-the-fly
côté navigateur. Donc un changement JSX ne nécessite ni rebuild ni restart, juste
un `Cmd + Shift + R` côté browser pour vider le cache JS.

---

## Procédure complète détaillée

### Étape 0 — Pre-flight

```bash
# Vérifie que tu n'as pas de WIP non commité oublié
cd "/Users/oscargrima/Documents/Claude/Projects/Polymarket trading bot/polymarket-bot"
git status

# Vérifie la branche (la prod déploie depuis n'importe quelle branche
# via rsync — git n'intervient pas dans le déploiement, seulement dans
# l'historique). Mais conviens d'une branche de référence.
git branch --show-current
```

### Étape 1 — Commit local

```bash
git add <fichiers ciblés>            # ou git add -A pour tout
git commit -m "<message clair>"
git push
```

Le push sert à archiver l'état dans le remote, pas à déclencher le déploiement.

### Étape 2 — Sync vers la VM

Le `rsync` est l'étape qui copie les fichiers de ton Mac vers la VM. Le
`--delete` supprime côté VM ce qui n'existe plus en local — important pour
éviter d'accumuler de vieux fichiers obsolètes.

```bash
rsync -avz --delete \
  --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.pytest_cache/' --exclude '.ruff_cache/' --exclude '.mypy_cache/' \
  --exclude '.venv/' --exclude 'venv/' --exclude '*.egg-info/' \
  --exclude 'data_cache/' --exclude 'reports/' \
  --exclude '.DS_Store' --exclude '.claude/' \
  --exclude '*.log' --exclude 'orchestrate.log' \
  --exclude '.env' --exclude '.env.local' --exclude '.env.*.local' \
  -e "ssh -i ~/.ssh/hetzner_polymarket" \
  ./ polymarket@89.167.23.215:/opt/polymarket-bot/
```

Important :
- ✅ `.env` est EXCLU — il contient les credentials prod, ne JAMAIS l'écraser depuis le Mac
- ✅ `data_cache/`, `*.log`, `reports/` exclus — bruit local
- ✅ `.git/` exclu — la VM n'est pas un repo
- ✅ `__pycache__/` et caches exclus — invalidés par le rebuild de toute façon

Le `-v` te liste tout ce qui a été transféré. Si tu vois beaucoup de fichiers
défiler, c'est normal après une grosse session ; si presque rien ne défile
alors que tu as modifié 10 fichiers, c'est suspect (vérifie tes timestamps
ou ton chemin source).

### Étape 3 — Rebuild + restart (sur la VM)

```bash
ssh -i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215
cd /opt/polymarket-bot
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate
```

`build` recompile l'image Docker. Avec le cache layer Docker, seules les
étapes invalidées rebuildent. Si tu n'as touché que `src/**/*.py`, le
rebuild prend 30 s à 1 min. Si tu as touché `pyproject.toml`, il refait
le venv complet (3-5 min).

`up -d --force-recreate` détruit puis recrée les containers avec la nouvelle
image. Le `-d` les détache du terminal.

Si le `build` échoue, **n'enchaîne pas avec `up -d`** sinon la prod tournerait
sur l'ancienne image silencieusement. Lis l'erreur, fix, et relance.

### Étape 4 — Vérifications post-deploy

```bash
# Containers tous Healthy (8 services)
docker ps --format "table {{.Names}}\t{{.Status}}"

# API basique
curl -s http://localhost:8080/healthz

# Endpoints critiques répondent
curl -s http://localhost:8080/api/risk/config | python3 -m json.tool | head -10
curl -s "http://localhost:8080/api/inspector/snapshot?limit=3" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('counters:', d.get('counters'))
"
curl -s http://localhost:8080/api/data-quality | python3 -m json.tool | head -20

# Logs récents (ctrl-C pour sortir)
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail=50 -f engine
```

Puis ouvre http://89.167.23.215:8080 dans le navigateur, hard refresh
(Cmd+Shift+R), et clique sur les onglets impactés pour valider visuellement.

---

## Migrations SQL ponctuelles (cleanup, fix data)

Pour exécuter un script SQL one-shot sans rebuild :

```bash
# Sur la VM
docker exec -i polymarket_db psql -U polymarket -d polymarket \
  < scripts/<nom_script>.sql
```

⚠️ **L'utilisateur Postgres est `polymarket`, pas `postgres`** (le défaut
Postgres) — tomber dans ce piège donne `FATAL: role "postgres" does not exist`.

Pour une commande SQL inline (sans fichier) :

```bash
docker exec -i polymarket_db psql -U polymarket -d polymarket <<'EOF'
SELECT COUNT(*) FROM leaders WHERE excluded = FALSE;
EOF
```

---

## Rollback en urgence

Si le deploy casse quelque chose en prod :

### Option A — Revert le commit + redeploy

```bash
# Mac
git log --oneline -5                          # repère le commit fautif
git revert <commit_sha>
git push

# Re-rsync + rebuild + up -d
rsync -avz --delete ... (cf. étape 2)
ssh hetzner-polymarket
cd /opt/polymarket-bot
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate
```

### Option B — Restart les containers sur l'image précédente

Docker garde les anciennes images quelques jours :

```bash
# Sur la VM
docker images polymarket-bot
# Repère le tag/SHA précédent

# Édite docker-compose.yml temporairement pour utiliser l'ancienne image
# OU re-tag manuellement :
docker tag polymarket-bot:<sha_précédent> polymarket-bot:latest
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate
```

### Option C — Killswitch immédiat

Si la prise de décision part en vrille, kill toutes les exécutions sans
toucher au code :

```bash
curl -X POST http://localhost:8080/api/control/killswitch \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false, "reason": "deploy-broke-something", "actor": "ops"}'
```

Le bot continue d'observer mais n'ouvre plus de positions paper ni live.

---

## Troubleshooting

### Le SSH retourne `Permission denied (publickey)`

```bash
# Vérifie que la clé existe
ls -la ~/.ssh/hetzner_polymarket

# Permissions correctes (sinon ssh refuse)
chmod 600 ~/.ssh/hetzner_polymarket

# Test
ssh -i ~/.ssh/hetzner_polymarket -v polymarket@89.167.23.215 'echo OK'
```

Si la clé n'existe pas du tout, tu dois la régénérer et la pousser dans
`~/.ssh/authorized_keys` du user `polymarket` sur la VM (passer par la
console Hetzner si tu n'as plus aucun accès SSH).

### Le `docker compose build` échoue sur `COPY ... not found`

Ça veut dire qu'un fichier est dans le `.dockerignore` ou n'existe pas
dans le build context. Vérifie le `.dockerignore` (notamment les rules
de négation `!docs/migrations/` doivent venir APRÈS `docs/`).

### Le `git pull` échoue sur la VM avec `not a git repository`

Normal — `/opt/polymarket-bot/` n'est pas un repo git. Le déploiement
passe par `rsync`, pas par `git pull`. Si tu veux un `git pull` direct
sur la VM, il faudrait `git init` dans `/opt/polymarket-bot/`, ajouter
le remote, et soit force-push depuis le local, soit merger l'arbre local
sans conflit. Pas indispensable tant que rsync fait le job.

### Le hard refresh navigateur ne montre pas les nouveaux JSX

Vérifie que le rsync a bien transféré `static/dashboard/*.jsx` :

```bash
# Sur la VM
ls -la /opt/polymarket-bot/static/dashboard/dashboard-tabs.jsx
# Compare le timestamp avec celui du Mac
```

Si le fichier est à jour côté VM mais le browser montre toujours l'ancien
contenu, ouvre les DevTools → Network → coche "Disable cache" → refresh.

### `psql: FATAL: role "postgres" does not exist`

Utilise `-U polymarket` au lieu de `-U postgres`. Le compose définit
`POSTGRES_USER=polymarket` donc le rôle par défaut `postgres` n'existe pas.

---

## Workflow alternatif (si on bascule sur git pull pur)

Pour formaliser le workflow et virer le rsync, on pourrait initialiser un
repo git dans `/opt/polymarket-bot/` :

```bash
# Sur la VM, UNE FOIS
cd /opt/polymarket-bot
git init
git remote add origin https://github.com/FiExplorer11020/Poybot.git
git fetch
git reset --hard origin/main
```

Ensuite le déploiement deviendrait :

```bash
# Mac
git push

# VM
ssh hetzner-polymarket
cd /opt/polymarket-bot
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate
```

Avantage : une seule source de vérité (git), historique de prod traçable.
Inconvénient : besoin de gérer les credentials git sur la VM (HTTPS token
ou clé SSH déployée). On peut envisager ce switch si le rsync devient
fastidieux ou si on veut activer du CI/CD.

---

## Script de déploiement automatisé (proposition)

Un futur `scripts/deploy.sh` pourrait enchaîner tout ça :

```bash
#!/usr/bin/env bash
set -euo pipefail

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Workspace not clean, commit first." >&2
  exit 1
fi

echo "→ git push"
git push

echo "→ rsync to VM"
rsync -avz --delete \
  --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.pytest_cache/' --exclude '.ruff_cache/' --exclude '.mypy_cache/' \
  --exclude '.venv/' --exclude 'venv/' --exclude '*.egg-info/' \
  --exclude 'data_cache/' --exclude 'reports/' \
  --exclude '.DS_Store' --exclude '.claude/' \
  --exclude '*.log' --exclude 'orchestrate.log' \
  --exclude '.env' --exclude '.env.local' --exclude '.env.*.local' \
  -e "ssh -i ~/.ssh/hetzner_polymarket" \
  ./ polymarket@89.167.23.215:/opt/polymarket-bot/

echo "→ rebuild + recreate on VM"
ssh -i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215 \
  'cd /opt/polymarket-bot && \
   docker compose -f docker-compose.yml -f docker-compose.prod.yml build && \
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate && \
   docker ps --format "table {{.Names}}\t{{.Status}}"'

echo "→ healthz"
sleep 10
curl -s http://89.167.23.215:8080/healthz

echo "✓ Deploy done."
```

À créer sur demande — pas livré encore.
