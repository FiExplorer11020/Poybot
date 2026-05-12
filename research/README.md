# Research Substrate — Round 13 (The Mirror)

This directory is the **operator + analyst** research workspace. It is
deliberately at the repo root (not under `src/`) because:

1. The notebooks read from the **cold tier** (R6 Parquet exports) +
   the live Postgres tables, but they never write back to production.
2. They run offline — no live API calls. Reproducible against a
   snapshotted DuckDB file.
3. They graduate from "experiments" to "operator runbook" when the
   findings get committed back as a new round-N spec or a runtime
   config change. The notebooks themselves stay git-tracked so the
   audit trail is preserved.

---

## Setup

```bash
# Working dir
cd research

# Install the pinned research environment (separate from production
# pyproject.toml — keeps the bot's runtime dep tree lean).
pip install -r requirements.txt

# (Optional) link the cold-tier Parquet directory so DuckDB can read it.
mkdir -p duckdb
ln -s /var/lib/polymarket-bot/cold_storage/parquet duckdb/cold
```

DuckDB is **embedded** — no server. Each notebook session creates its
own `duckdb/research.duckdb` working file. That file is `.gitignore`d;
the notebooks are not.

---

## Notebooks

| File | Purpose | Wall time |
|---|---|---|
| `00_data_loader.ipynb` | DuckDB views over the cold tier. Reusable cell-1 setup for the others. | < 30 s |
| `01_strategy_classifier_validation.ipynb` | Re-validates R8 against held-out hand-labels; surfaces wallets where classifier disagrees with manual labels. | ~1 min |
| `02_causal_analysis.ipynb` | Plots R10 IV vs R9 Hawkes disagreement; surfaces (leader, pool) pairs where statistical and causal estimates diverge most. | ~2 min |
| `03_counterfactual_replay.ipynb` | Interactive what-if: change a runtime parameter, replay last 30 days, compute hypothetical Sharpe diff. Uses R10's `CounterfactualReplayer`. | < 5 min (spec § 3.4 gate) |
| `04_what_if_explorer.ipynb` | Per-hypothesis explorer: "what if R8 had 10 classes for X?", "what if R11 had shipped a quarter earlier?" | varies |
| `05_calibration_review.ipynb` | Reads `calibration_loss_history`; per-model drift trajectories; analyst's auto-disable triage tool. | < 30 s |

Each notebook starts with a "what this notebook does" markdown cell
and a parameters cell where the analyst can override defaults
(dates, model versions, wallet filters).

---

## When data is missing

The notebooks degrade gracefully — every analysis cell guards against
empty results from the cold tier or live tables, displaying a clear
"no data — populate via X" message rather than crashing. This is
deliberate: a brand-new operator should be able to clone the repo,
run the notebooks immediately, and see the path to populating the
inputs they need.

---

## Reproducibility

`research/requirements.txt` pins versions. The cold-tier Parquet
schema is documented in `docs/ROUND_6_THE_SPINE.md` § 3.6. The
DuckDB file is reproducible from the Parquet snapshot + Postgres
dump. A notebook that produced a finding 6 months ago should produce
the same finding today if rerun against the same snapshot.

---

## Forward path

When a notebook finding becomes actionable:

1. Distill the analysis into a runtime config change or new model
   threshold.
2. Open a PR with the change + a brief mention of which notebook +
   which cell produced the finding.
3. The notebook stays as the audit trail. If the finding is later
   contradicted, the notebook is the diff target.
