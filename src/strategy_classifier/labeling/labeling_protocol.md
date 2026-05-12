# Strategy Labelling Protocol — Round 8 (The Lens)

This is the operator-facing guide for the **hand-labelling sprint** that
seeds the supervised classifier. The full design rationale lives in
`docs/ROUND_8_STRATEGY_CLASSIFIER.md` §§ 2-3.2.

The labelling tools themselves (the `batch_labeler.ipynb` Jupyter
notebook) are operator deliverables — they are NOT in the code tree.
This document is the contract between code and operator.

## Targets

- **100 labelled (wallet, 30-day-window) pairs** stored in
  `strategy_labels`. Stratified across the 9 classes — minimum 5 in
  every class, even the rare ones (`info_leak`, `arb_3way`).
- **20-wallet validation pair-label set**: operator + second labeller
  label the same 20 wallets independently. Cohen's κ measured via
  `StrategyLabelStore.compute_inter_labeller_kappa(labeller_a, labeller_b)`.
  **Gate: κ ≥ 0.7** before unlocking the remaining 80 labels.

## The 9 classes

Use the names exactly as written. The DB CHECK constraint rejects
typos:

| Class | One-line signature |
|---|---|
| `directional` | Days-to-weeks holding period, conviction trades, low cancel-to-fill |
| `momentum` | Enters AFTER price moves, hours holding, high volume |
| `contrarian` | Enters AGAINST price moves, longer holding |
| `arb_2way` | Symmetric YES+NO positions, exits on mid-convergence |
| `arb_3way` | Cross-market or cross-token arbitrage |
| `market_maker` | Tight spreads both sides, frequent quotes, low fill rate |
| `structural_bot` | < 100 ms decision latency, deterministic patterns |
| `info_leak` | Entries cluster minutes after news; rare, high-edge |
| `social_driven` | Entries cluster with X/Telegram posting velocity |

## Per-wallet workflow

For each candidate wallet:

1. Open the wallet's profile page (dashboard → Wallet Scanner →
   click the row).
2. Pick the **most recent 30-day window** where the wallet was active
   (≥ 10 trades). This is the `label_window_start` →
   `label_window_end` pair.
3. Inspect:
   - Holding period distribution (the Wallet Scanner table's "avg
     holding" column).
   - Category mix (preferred_categories Dirichlet posterior).
   - Time-of-day KDE.
   - Trade velocity (trades/day, p99 inter-trade interval).
   - Falcon Score + Wallet 360 metrics (raw JSON in `wallet360_json`).
4. Pick `primary_strategy`. If the wallet has a clear secondary mode
   (e.g., 70 % directional + 30 % info_leak), set
   `secondary_strategy` too.
5. Set `confidence` to your subjective 0-1 confidence in the label.
   < 0.6 = "unsure, taxonomy may need a new class for this one".
6. Write a one-sentence `rationale` explaining the signature you
   keyed on (e.g., "held > 6h on every closed position, low cancel
   activity, no social signature").
7. Insert via `StrategyLabelStore.insert_label(LabelRow(...))`.

## The 20-wallet validation sprint (BEFORE the main 80)

1. Operator picks 20 wallets — try to spread across all 9 classes.
2. Operator + second labeller each label all 20 independently. Do
   NOT share labels until both are done.
3. Each labeller uses a distinct `labeller` string (e.g., "op_alice",
   "op_bob").
4. Compute κ:

   ```python
   from src.strategy_classifier.labeling import StrategyLabelStore
   store = StrategyLabelStore()
   result = await store.compute_inter_labeller_kappa("op_alice", "op_bob")
   print(result["kappa"], result["agreement_rate"])
   ```

5. **Gate: κ ≥ 0.7**.
   - If κ < 0.7: review the disagreements (`result["labels"]`).
     Common drivers: ambiguity between `momentum` ↔ `directional`
     (both can have long holding periods), and `info_leak` ↔
     `social_driven` (both react to external signals). Refine this
     protocol document and re-label, OR add a clarifying example
     in the rubric.
   - If κ ≥ 0.7: proceed.

## The main 80-wallet pass (after the gate)

1. Operator labels 80 more wallets, stratified to target ≥ 10 per
   class.
2. After the pass: 5-wallet spot-check by the second labeller. If
   any spot-check label disagrees with the operator and the operator
   can't justify the discrepancy, ESCALATE — that's a taxonomy
   issue, not a labelling-mistake issue.

## What you do NOT do

- Don't label structural_bots. Those are excluded by registry already
  (`leaders.excluded = TRUE`). They're in the taxonomy only as a
  defence-in-depth class for the daemon to detect-and-flag a wallet
  that slipped through. Stick to wallets where `excluded = FALSE`.
- Don't relabel a wallet to "fix" it. INSERT a new row; the latest
  `labelled_at` wins (the store is append-only by design).
- Don't label fewer than 5 in any class. The classifier will overfit
  on the majority class. If you can't find 5 examples in `info_leak`,
  surface the problem to engineering — that may indicate the class
  itself doesn't exist in the data and the taxonomy needs revision.

## Reference rubric — fine-grained discriminators

A few common confusions, resolved:

- **`directional` vs `momentum`**: Momentum traders ALWAYS enter
  *after* the move — check the entry timestamps vs the candle data.
  Directional traders enter *before* / *during* and ride; their
  holding period is also typically 3-5× longer.
- **`info_leak` vs `social_driven`**: info_leak entries cluster
  *minutes after a news event* (cross-ref the news timeline in the
  market detail panel). social_driven entries cluster *minutes after
  a tweet* (R12 cross-ref; if R12 isn't live yet, default to
  info_leak when in doubt and note the rationale).
- **`arb_2way` vs `arb_3way`**: 2-way is YES+NO of the *same* market.
  3-way is cross-market (e.g., "Trump win" YES + "Biden win" NO).
  3-way is much rarer; if you're not certain, default to 2-way.

## Operator-only gates (NOT in code)

- Sprint timing — the spec budgets 1 dedicated week.
- Cohen's κ measurement.
- Decision whether to extend the taxonomy after the unsupervised
  explorer surfaces a candidate cluster (`UnsupervisedStrategyExplorer.surface_candidate_clusters`).
- A/B test of `strategy_conditional_confidence_enabled` after the
  classifier is trained.

These are all explicitly out of scope for the R8 code-layer drop.
