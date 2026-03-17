# Audit de l'architecture réseau — Poybot

## 1) Résumé exécutif

Le MVP présente une architecture réseau simple et fonctionnelle pour un environnement local, mais **pas encore durcie pour une exposition Internet**.

Points clés :
- Architecture claire : frontend Next.js (3000) ↔ backend FastAPI (8000) ↔ PostgreSQL/Redis.
- Dépendances externes critiques : APIs Polymarket (Gamma REST, CLOB REST, CLOB WebSocket).
- Surface d'exposition excessive en `docker-compose` (PostgreSQL, Redis, ClickHouse publiés sur l'hôte).
- Contrôles réseau manquants côté API/WS (authentification, limitation de débit, sécurité transport).

Niveau de risque global (prod): **Moyen à Élevé**.

---

## 2) Périmètre audité

- Backend FastAPI + workers ARQ + ingestion websocket.
- Frontend Next.js + bridge websocket.
- Orchestration locale Docker Compose.
- Variables de configuration réseau.

---

## 3) Cartographie des flux réseau

## 3.1 Flux entrants (north-south)

- Client navigateur → Frontend Next.js : `:3000`.
- Client navigateur → Backend FastAPI REST : `:8000`.
- Client navigateur → Backend FastAPI WebSocket : `/ws/live`.

## 3.2 Flux internes (east-west)

- Backend API/worker → PostgreSQL (`5432`).
- Worker ARQ → Redis (`6379`) pour queue/cron.
- Backend/worker → (potentiellement) ClickHouse (`8123/9000`) exposé mais non essentiel dans les flux applicatifs observés.

## 3.3 Flux sortants (egress)

- Backend/worker → Polymarket Gamma REST (`https://gamma-api.polymarket.com`).
- Backend/worker → Polymarket CLOB REST (`https://clob.polymarket.com`).
- Ingestor backend → Polymarket CLOB WebSocket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`).
- Frontend wallet stack → transport RPC Polygon via wagmi/viem (provider HTTP par défaut) + WalletConnect cloud project ID.

---

## 4) Constat détaillé

### 4.1 Exposition des services de données en local

Le `docker-compose` mappe PostgreSQL, Redis et ClickHouse sur toutes les interfaces hôtes (`5432`, `6379`, `8123`, `9000`). Pour un poste développeur partagé, cela élargit inutilement la surface d'attaque.

**Impact**: accès non désiré local/réseau voisin, brute force, extraction de données si machine exposée.

### 4.2 Secrets et credentials faibles par défaut

- PostgreSQL en `postgres/postgres` dans la composition locale.
- Redis sans mot de passe.
- Paramètres applicatifs avec valeurs par défaut orientées dev.

**Impact**: compromission triviale si ports accessibles.

### 4.3 API et WebSocket sans contrôle d'accès explicite

- Endpoint WS public `/ws/live` sans authentification.
- Routes REST de contrôle bot (`/api/v1/bot/control`) sans couche d'authn/authz visible.

**Impact**: en cas d'exposition externe, prise de contrôle fonctionnelle du bot et observation des données live.

### 4.4 Sécurité transport et reverse proxy

- Backend servi en HTTP clair (`uvicorn --host 0.0.0.0 --port 8000`), pas de terminaison TLS native.
- Aucune configuration de proxy/API gateway (Nginx/Traefik/Caddy) observée.

**Impact**: chiffrement, en-têtes de sécurité et protections L7 non assurés en production brute.

### 4.5 Résilience réseau côté dépendances externes

- Clients HTTP (`httpx`) avec timeout, mais pas de stratégie de retry/backoff centralisée.
- Ingestor WS reconnecte en boucle avec pause fixe (2s), sans jitter ni limites.

**Impact**: comportement instable lors d'incident fournisseur, possible effet "thundering herd" et saturation.

### 4.6 Gouvernance des flux sortants

Les domaines de sortie sont bien centralisés dans la configuration, mais il manque une politique explicite d'allow-list egress au niveau infra.

**Impact**: difficulté de contrôle de conformité/réduction du blast radius.

### 4.7 Frontend: paramètres wallet

Le frontend accepte une valeur de secours `demo-project-id` pour WalletConnect.

**Impact**: fiabilité limitée, quotas/latence variables, traçabilité faible en environnement réel.

---

## 5) Recommandations priorisées

## P0 (immédiat avant exposition publique)

1. **Ne plus publier DB/Redis/ClickHouse vers l'hôte** sauf besoin strict dev (`expose` interne Docker plutôt que `ports`).
2. **Ajouter authn/authz** sur les routes sensibles et le WS live (JWT/session, scopes).
3. **Passer derrière un reverse proxy TLS** (HTTPS/WSS) + règles IP/rate limiting.
4. **Supprimer les credentials par défaut** et imposer des secrets forts via `.env`/secret manager.

## P1 (court terme)

1. Implémenter **retry avec backoff exponentiel + jitter** sur clients REST/WS externes.
2. Ajouter **timeouts/limits** explicites (connexion, lecture, pool) sur HTTPX et websocket.
3. Mettre en place une **allow-list egress** (Gamma/CLOB/RPC nécessaires uniquement).
4. Activer journalisation sécurité (connexion, erreurs auth, throttling) + métriques réseau.

## P2 (maturité)

1. Segmenter les réseaux Docker (frontend, app, data).
2. Introduire un WAF/API gateway et politiques Zero Trust inter-services.
3. Basculer la config sensible vers un secret manager (Vault, SSM, etc.).
4. Tester régulièrement via scans (ports, dépendances, SAST/DAST minimal).

---

## 6) Cible d'architecture recommandée (prod)

- Internet → Reverse Proxy/API Gateway (TLS, WAF, rate limit, auth) → Backend API/WS.
- Backend/worker sur réseau privé.
- PostgreSQL/Redis/ClickHouse sans publication de ports publics.
- Egress restreint à une allow-list de domaines Polymarket + RPC wallet.
- Observabilité : métriques latence API/WS, erreurs réseau externes, saturation pool DB/Redis.

---

## 7) Conclusion

L'architecture réseau actuelle est cohérente pour un MVP local, mais nécessite un **durcissement important** avant toute ouverture hors environnement de développement. Les actions P0 ci-dessus réduisent rapidement le risque principal (exposition de services d'infrastructure et absence de contrôles d'accès applicatifs).
