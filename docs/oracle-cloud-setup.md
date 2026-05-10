# [ARCHIVED] Oracle Cloud setup

> **This document is archived.** The bot is no longer hosted on Oracle Cloud.
>
> The S5 deployment migrated to **Hetzner Cloud (Helsinki, HEL1)** in early
> May 2026 after 5 days of constant CPU saturation on the Ampere A1 free
> tier in `eu-paris-1`. France is also a Polymarket-blocked geography,
> which made `eu-paris-1` problematic for the trading path.
>
> **Current production VM:** `polymarket-prod` at `89.167.23.215` (Hetzner
> CX23, Ubuntu 22.04, 2 vCPU AMD / 4 GB / 40 GB SSD). Provisioning notes
> are in [INFRA.md](INFRA.md) and the deployment workflow is in
> [docker-setup.md](docker-setup.md).
>
> The Oracle steps below are kept for historical reference only. Do not
> follow them for new provisioning — they will not match the current
> stack (memory budgets, network rules, packaging) and the always-free
> tier in some regions has been deprecated since this guide was written.

---

## Original content (do not act on)

The original `oracle-cloud-setup.md` walked through:

- Generating a dedicated SSH keypair (`ssh-keygen -t ed25519 -f ~/.ssh/oracle_polymarket`)
- Creating an Always-Free Ampere A1 VM (4 OCPU / 24 GB / `eu-paris-1`)
- Configuring the OCI security list to allow only the operator's IP
- Installing Docker + docker-compose-plugin
- Cloning the repo, copying `.env`, and running the prod overlay

The equivalent Hetzner-flavoured procedure is documented in
[INFRA.md](INFRA.md) and [docker-setup.md](docker-setup.md). The trading
geography constraints (France blocked, Germany trading-restricted, Finland
OK) drove the Helsinki choice; see the "Pourquoi Helsinki" section in
INFRA.md for the rationale.

If you ever need to migrate again, the items that need to change in this
repo are:

1. `.env`'s `DATABASE_URL` / `REDIS_URL` (only matters for native
   non-Docker runs; compose injects service DNS).
2. The IPs in the UFW allowlist on the VM (operator IP for SSH).
3. The `STATIC_DIR` / templates path are repo-relative, so they survive
   a host change without touching the code.
4. The compose memory caps in `docker-compose.prod.yml` if you move to a
   different VM size.

The image itself (`polymarket-bot:latest`, multi-stage Dockerfile) is
host-agnostic.
