# Oracle Cloud — provisioning de la VM (S5.13)

Cible : **Ampere A1 / 4 OCPU / 24 GB RAM, Ubuntu 22.04 ARM64, eu-paris-1**.
Free-tier permanent tant que les ressources restent dans les bornes
always-free (1 boot vol < 200 GB, 4 OCPU Ampere total cumulé sur le
compte, etc.).

---

## Avant de cliquer dans la console

### 1. Générer une keypair SSH dédiée (sur ta machine locale)

Les clés SSH par défaut (`~/.ssh/id_rsa`) marchent mais on isole les
clés cloud pour pouvoir les révoquer indépendamment.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/oracle_polymarket -C "polymarket-bot@oracle"
```

Laisse la passphrase vide si la machine est physiquement sécurisée
(ou utilise `ssh-agent` sinon). Ça produit deux fichiers :

- `~/.ssh/oracle_polymarket` — clé privée (à garder)
- `~/.ssh/oracle_polymarket.pub` — clé publique (à coller dans Oracle)

### 2. Récupérer ta clé publique au format Oracle attend

```bash
cat ~/.ssh/oracle_polymarket.pub
# Sortie : ssh-ed25519 AAAA... polymarket-bot@oracle
```

Garde ça dans le presse-papier.

### 3. Récupérer ton IP publique (pour le firewall)

```bash
curl ifconfig.me
```

Note-la. On limitera SSH à cette IP (plus la peine que le port 22 soit
ouvert au monde entier).

---

## Provisioning via la console Oracle Cloud

### Étape A — Créer un compartment

Compartments Oracle = unités d'isolation (équivalent project AWS/GCP).
Ce n'est pas obligatoire mais ça évite de polluer le compartment root.

1. Console → menu hamburger → **Identity & Security → Compartments**.
2. **Create Compartment**.
   - Name : `polymarket-bot`
   - Description : `bot trading 24/7`
   - Parent compartment : laisser le compartment root
3. Create.

Tous les clics suivants se feront dans ce compartment (sélecteur en
bas à gauche de la console).

### Étape B — Créer un VCN (réseau)

Le wizard "Start VCN Wizard" crée tout l'écosystème réseau d'un coup :
VCN + subnet public + Internet Gateway + Route Table + Security List.

1. Console → menu → **Networking → Virtual Cloud Networks**.
2. Vérifie en haut à droite que la région est **EU Paris (eu-paris-1)**.
3. **Start VCN Wizard**.
4. Choisis **"Create VCN with Internet Connectivity"**, Start workflow.
5. Settings :
   - VCN name : `polymarket-vcn`
   - Compartment : `polymarket-bot`
   - VCN CIDR : `10.0.0.0/16` (default)
   - Public subnet CIDR : `10.0.0.0/24` (default)
   - Use DNS hostnames : ✅
6. Next → Create.

Tu devrais voir, en quelques secondes : VCN, subnet public, IGW,
route table, Security List default. Tous bons.

### Étape C — Adapter la Security List (firewall réseau)

La Security List default ouvre le port 22 au monde (0.0.0.0/0). On
restreint à ton IP, et on ouvre le 8080 pour le dashboard.

1. Dans le VCN qu'on vient de créer → **Security Lists** → "Default
   Security List for polymarket-vcn".
2. **Ingress Rules** → on a déjà une règle `0.0.0.0/0 TCP/22`.
   - Edit → remplace `0.0.0.0/0` par `<TON_IP>/32`. Save.
3. **Add Ingress Rules** :

| Stateless | Source CIDR    | IP Protocol | Source Port | Destination Port | Description           |
|-----------|----------------|-------------|-------------|------------------|-----------------------|
| ❌        | `<TON_IP>/32`  | TCP         | All         | `8080`           | API dashboard         |

> ⚠️ **Pas besoin d'ouvrir 5432/6379** — Postgres et Redis sont
> uniquement sur le réseau interne Docker. Le compose-prod retire
> même les host port mappings (`!reset []`).

4. Add Ingress Rules → Save.

### Étape D — Créer l'instance compute (la VM)

1. Console → menu → **Compute → Instances** → **Create instance**.
2. Compartment : `polymarket-bot`. Name : `polymarket-prod`.
3. **Image and shape** → Edit.
   - **Image** : Change image → **Canonical Ubuntu** → **22.04** →
     coche bien "**aarch64**" (ARM64). Select.
   - **Shape** : Change shape → **Ampere** → `VM.Standard.A1.Flex`.
     - OCPUs : `4`
     - Memory : `24` GB
   - Select shape.

   > 🔥 **C'est ici que tu vas peut-être croiser "Out of host capacity"**.
   > Si oui : retry dans quelques minutes / heures, ou bascule en
   > eu-frankfurt-1 (refais B/C/D dans cette région).

4. **Networking** → expand.
   - VCN : `polymarket-vcn`
   - Subnet : le subnet public.
   - Public IP : **Assign a public IPv4 address** (on en réserve une
     statique juste après).
5. **Add SSH keys** → "Paste public keys" → colle le contenu de
   `~/.ssh/oracle_polymarket.pub`.
6. **Boot volume** → laisse le default (50 GB suffisent largement,
   on est dans la limite gratuite).
7. **Show advanced options** → onglet **Management**.
   - **Initialization script** → Paste cloud-init script → colle le
     **contenu intégral** de `scripts/oracle_cloud_init.yml`. C'est
     ce qui installe Docker, ouvre iptables, crée le swap, etc.
8. **Create**.

L'instance passe par PROVISIONING → STARTING → RUNNING en ~2 minutes.
cloud-init prend encore ~3-5 min en plus pour finir d'installer
Docker. Patience.

### Étape E — Réserver une IP publique statique

Par défaut l'IP publique est éphémère (perdue à chaque stop). On la
fige.

1. Sur la page de l'instance → onglet **Resources → Attached VNICs**
   → clique le VNIC primaire.
2. **Resources → IPv4 Addresses** → l'IP publique éphémère est listée.
   "Edit" (à droite) → **Reserved Public IP** → **Create new
   reserved IP** → Name : `polymarket-prod-ip`. Update.

Note l'IP qui apparaît — c'est celle qu'on utilisera pour SSH et
DNS. Elle ne changera plus.

---

## Configuration locale (sur ta machine)

### Étape F — `~/.ssh/config` alias

Évite de retaper l'IP/clé/user à chaque SSH.

```
# ~/.ssh/config
Host polymarket-prod
    HostName <IP_RESERVÉE>
    User ubuntu
    IdentityFile ~/.ssh/oracle_polymarket
    IdentitiesOnly yes
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

Test :

```bash
ssh polymarket-prod 'cat /var/log/cloud-init-bot.log'
# Tu dois voir : cloud-init OK — 2026-04-29T...
#                Docker version 26.x.x
#                Docker Compose version v2.x.x
```

Si tu ne vois pas ça, attends encore 2-3 min — cloud-init n'a pas
fini. Si après 10 min toujours rien : `sudo cat /var/log/cloud-init-output.log`
pour voir où ça a coincé.

### Étape G — Bootstrap du projet

Sur ton laptop :

```bash
# 1. Push une .env de prod sur la VM. NE COMMITE JAMAIS .env.
scp polymarket-bot/.env polymarket-prod:/opt/polymarket-bot/.env

# 2. SSH + lance le post-install script.
ssh polymarket-prod
cd /opt/polymarket-bot
git clone <TON_REPO_URL> .   # ou : scp -r polymarket-bot/* polymarket-prod:/opt/polymarket-bot/
bash scripts/oracle_post_ssh.sh
```

Le script vérifie cloud-init, monte postgres + redis, et te dit ce
qu'il reste à faire (migrations, etc.).

---

## Smoke-tests rapides

Après le bootstrap :

```bash
# Sur la VM
docker compose ps
# Doit montrer postgres + redis en "healthy"

docker compose logs --tail=20 postgres
# Doit finir par "database system is ready to accept connections"

docker compose run --rm engine python -c "from src.config import settings; print(settings.DATABASE_URL)"
# Sanity check de la config
```

L'API n'est pas encore lancée à ce stade — c'est le but de S5.14
(migrations + restore depuis backup local) puis S5.15 (smoke test
complet + apps up).

---

## Ce qui reste après S5.13

- **S5.14** : Migrer la DB locale → cloud (`pg_dump` local, `scp`,
  `pg_restore` distant), puis verify.
- **S5.15** : `docker compose up -d` complet, observer 24h en mode
  paper, dashboard accessible sur `http://<IP>:8080`, alertes
  Telegram OK.

---

## Notes sécurité

- **Clé privée Oracle** : `~/.ssh/oracle_polymarket` ne doit jamais
  quitter ta machine. Backup chiffré (1Password / iCloud Keychain).
- **`.env` sur la VM** : permissions `600`, possédé par `ubuntu`. Le
  `oracle_post_ssh.sh` le force.
- **Mises à jour** : `unattended-upgrades` est activé pour les
  patches sécurité Ubuntu, reboot à 4h UTC si kernel update.
- **iptables** : double layer (Oracle Security List + iptables interne).
  Une nouvelle règle de port doit être ajoutée AUX DEUX endroits
  sinon ça ne passe pas.
- **Snapshot du boot volume** : prend un snapshot manuel après le
  smoke test S5.15 (Compute → Boot Volumes → ton volume → Create
  Manual Backup). Restore en 5 min si la VM est corrompue.
