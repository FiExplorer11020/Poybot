#!/usr/bin/env bash
# ===========================================================================
# Auto-retry launch d'une instance Ampere A1 sur OCI eu-paris-1.
#
# Pourquoi : la shape VM.Standard.A1.Flex est en saturation permanente dans
# eu-paris-1. On loop toutes les RETRY_INTERVAL_S secondes jusqu'à ce
# qu'Oracle libère de la capacité, puis on notifie via macOS notification
# center.
#
# Prérequis (déjà OK chez Oscar) :
#   - oci-cli installé et authentifié (`oci iam region list` doit marcher)
#   - SSH pubkey à ~/.ssh/oracle_polymarket.pub
#   - cloud-init userdata à scripts/oracle_cloud_init.yml
#   - VCN polymarket-vcn créé en eu-paris-1 avec un subnet public
#
# Usage :
#   cd /Users/oscargrima/Documents/Claude/Projects/Polymarket\ trading\ bot/polymarket-bot
#   bash scripts/oracle_retry_launch.sh
#
# Pour le lancer en background et garder le terminal libre :
#   nohup bash scripts/oracle_retry_launch.sh > /tmp/oracle_retry.log 2>&1 &
#   tail -f /tmp/oracle_retry.log
# ===========================================================================
set -euo pipefail

# --------------------------------------------------------------------------- #
# Config — adapte si besoin                                                    #
# --------------------------------------------------------------------------- #
COMPARTMENT_ID="ocid1.tenancy.oc1..aaaaaaaako5f7ceqx7zhvmlnasffbrhduw2dfwmkadsbjwpdblngegmczq2a"
VCN_NAME="polymarket-vcn"
DISPLAY_NAME="polymarket-prod"
SHAPE="VM.Standard.A1.Flex"
# eu-paris-1 saturée — on prend ce qui passe : 1 OCPU / 6 GB.
# Resize possible à chaud via la console une fois l'instance UP (Compute →
# Instance → Edit shape → 2/12 ou 4/24 → Save). Tester d'abord en small.
OCPUS=1
MEMORY_GB=6
BOOT_VOLUME_GB=50
SSH_PUBKEY="${HOME}/.ssh/oracle_polymarket.pub"

# Cloud-init et clés au chemin où tu lances le script (donc le repo).
CLOUD_INIT_YML="$(cd "$(dirname "$0")/.." && pwd)/scripts/oracle_cloud_init.yml"

RETRY_INTERVAL_S=300   # 5 min — sous le seuil rate-limit "Too many requests for the tenant"
MAX_RETRIES=0          # 0 = infini

LOG_PREFIX="[oci-retry]"
log() { echo "$(date -u +%FT%TZ) $LOG_PREFIX $*"; }
fail() { echo "$(date -u +%FT%TZ) $LOG_PREFIX ERROR: $*" >&2; exit 1; }

notify_mac() {
    local title="$1"; local message="$2"; local sound="${3:-Glass}"
    osascript -e "display notification \"${message}\" with title \"${title}\" sound name \"${sound}\"" 2>/dev/null || true
}

# --------------------------------------------------------------------------- #
# 1. Sanity checks                                                             #
# --------------------------------------------------------------------------- #
log "checking prerequisites..."
command -v oci >/dev/null || fail "oci-cli not found"
[[ -f "$SSH_PUBKEY" ]]    || fail "SSH pubkey missing at $SSH_PUBKEY"
[[ -f "$CLOUD_INIT_YML" ]] || fail "cloud-init yml missing at $CLOUD_INIT_YML"

oci iam region list >/dev/null 2>&1 || fail "OCI auth broken — run: oci iam region list"

# --------------------------------------------------------------------------- #
# 2. Discover OCIDs (AD, image, subnet)                                        #
# --------------------------------------------------------------------------- #
log "looking up availability domain in eu-paris-1..."
AD_NAME=$(oci iam availability-domain list \
    --compartment-id "$COMPARTMENT_ID" \
    --query 'data[0].name' --raw-output)
[[ -n "$AD_NAME" ]] || fail "couldn't find any AD in eu-paris-1"
log "  AD = $AD_NAME"

log "looking up Ubuntu 22.04 Minimal aarch64 image OCID..."
IMAGE_ID=$(oci compute image list \
    --compartment-id "$COMPARTMENT_ID" \
    --operating-system "Canonical Ubuntu" \
    --operating-system-version "22.04 Minimal aarch64" \
    --shape "$SHAPE" \
    --sort-by TIMECREATED --sort-order DESC \
    --query 'data[0].id' --raw-output)
if [[ -z "$IMAGE_ID" || "$IMAGE_ID" == "null" ]]; then
    log "  Minimal aarch64 not found — falling back to standard 22.04 aarch64"
    IMAGE_ID=$(oci compute image list \
        --compartment-id "$COMPARTMENT_ID" \
        --operating-system "Canonical Ubuntu" \
        --operating-system-version "22.04" \
        --shape "$SHAPE" \
        --sort-by TIMECREATED --sort-order DESC \
        --query 'data[0].id' --raw-output)
fi
[[ -n "$IMAGE_ID" && "$IMAGE_ID" != "null" ]] || fail "no Ubuntu 22.04 aarch64 image found"
log "  IMAGE = $IMAGE_ID"

log "looking up subnet inside $VCN_NAME..."
VCN_ID=$(oci network vcn list \
    --compartment-id "$COMPARTMENT_ID" \
    --display-name "$VCN_NAME" \
    --query 'data[0].id' --raw-output)
[[ -n "$VCN_ID" && "$VCN_ID" != "null" ]] || fail "VCN $VCN_NAME not found"

SUBNET_ID=$(oci network subnet list \
    --compartment-id "$COMPARTMENT_ID" \
    --vcn-id "$VCN_ID" \
    --query 'data[0].id' --raw-output)
[[ -n "$SUBNET_ID" && "$SUBNET_ID" != "null" ]] || fail "no subnet found in $VCN_NAME"
log "  SUBNET = $SUBNET_ID"

# --------------------------------------------------------------------------- #
# 3. Retry loop                                                                #
# --------------------------------------------------------------------------- #
log "starting retry loop — interval ${RETRY_INTERVAL_S}s, max=${MAX_RETRIES} (0=infinite)"
log "shape=$SHAPE ocpus=$OCPUS memory=${MEMORY_GB}GB boot=${BOOT_VOLUME_GB}GB"

attempt=0
while :; do
    attempt=$((attempt+1))
    log "attempt #$attempt — calling instance launch..."

    set +e
    OUTPUT=$(oci compute instance launch \
        --availability-domain "$AD_NAME" \
        --compartment-id "$COMPARTMENT_ID" \
        --shape "$SHAPE" \
        --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMORY_GB}" \
        --image-id "$IMAGE_ID" \
        --subnet-id "$SUBNET_ID" \
        --assign-public-ip true \
        --display-name "$DISPLAY_NAME" \
        --boot-volume-size-in-gbs "$BOOT_VOLUME_GB" \
        --ssh-authorized-keys-file "$SSH_PUBKEY" \
        --user-data-file "$CLOUD_INIT_YML" \
        --wait-for-state RUNNING \
        --max-wait-seconds 600 \
        2>&1)
    rc=$?
    set -e

    if [[ $rc -eq 0 ]]; then
        log "🎉 INSTANCE CREATED!"
        echo "$OUTPUT" | tee /tmp/oracle_instance.json
        INSTANCE_ID=$(echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',{}).get('id',''))" 2>/dev/null || echo "")
        if [[ -n "$INSTANCE_ID" ]]; then
            log "instance OCID = $INSTANCE_ID"
            log "fetching public IP..."
            sleep 5
            VNIC_ID=$(oci compute instance list-vnics --instance-id "$INSTANCE_ID" --query 'data[0].id' --raw-output 2>/dev/null || echo "")
            if [[ -n "$VNIC_ID" ]]; then
                PUBLIC_IP=$(oci network vnic get --vnic-id "$VNIC_ID" --query 'data."public-ip"' --raw-output 2>/dev/null || echo "")
                log "public IP = $PUBLIC_IP"
                notify_mac "Oracle Cloud ✅" "Instance up — IP $PUBLIC_IP"
                echo ""
                echo "============================================================"
                echo "  ✅  $DISPLAY_NAME is RUNNING"
                echo "  IP : $PUBLIC_IP"
                echo "  SSH: ssh ubuntu@$PUBLIC_IP -i $HOME/.ssh/oracle_polymarket"
                echo "============================================================"
                echo ""
                echo "Étape suivante : réserver l'IP publique en statique dans la console,"
                echo "ajouter le bloc ssh config, puis lancer scripts/oracle_post_ssh.sh"
            else
                notify_mac "Oracle Cloud ✅" "Instance up — get the IP from the console"
            fi
        else
            notify_mac "Oracle Cloud ✅" "Instance créée mais OCID introuvable, vérifie la console"
        fi
        exit 0
    fi

    # Detect capacity issues vs real errors
    if echo "$OUTPUT" | grep -qiE "out of (host )?capacity|InternalError|TooManyRequests|429"; then
        log "  capacity miss — retrying in ${RETRY_INTERVAL_S}s"
    elif echo "$OUTPUT" | grep -qiE "connection.*timed out|RequestException|ConnectionError|ConnectTimeout|ReadTimeout|503|502|504|temporarily unavailable"; then
        log "  ⏳ network/transient error — retrying in ${RETRY_INTERVAL_S}s"
    elif echo "$OUTPUT" | grep -qi "LimitExceeded"; then
        log "  ⚠️  LimitExceeded — peut-être quota de free tier dépassé. Détail :"
        echo "$OUTPUT" | head -20 >&2
        notify_mac "Oracle Cloud ⚠️" "LimitExceeded — vérifie tes quotas"
        fail "stop — vérifie tes limites Always Free"
    else
        log "  ❌ unexpected error :"
        echo "$OUTPUT" | head -30 >&2
        notify_mac "Oracle Cloud ❌" "Erreur inattendue — voir le log"
        fail "stop — erreur non récupérable, voir au-dessus"
    fi

    if [[ $MAX_RETRIES -gt 0 && $attempt -ge $MAX_RETRIES ]]; then
        notify_mac "Oracle Cloud ⌛" "Plus de capacité après $MAX_RETRIES essais — abandon"
        fail "max retries reached"
    fi

    # Heartbeat visible — tu vois clairement que le script vit pendant l'attente.
    for i in $(seq "$RETRY_INTERVAL_S" -1 1); do
        printf "\r$(date -u +%FT%TZ) $LOG_PREFIX   next retry in %02ds (Ctrl+C pour arrêter)   " "$i"
        sleep 1
    done
    printf "\r%80s\r" " "   # nettoie la ligne
done
