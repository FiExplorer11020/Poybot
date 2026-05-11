# Round 12 — Social Signal + Cross-Market Index

> **Formal title**: Off-Chain Pre-Signal & Multi-Venue Wallet Resolution
> **Colloquial name**: The Periphery
> **Prerequisite**: Round 6 (daemon framework + cold tier), Round 8
> (strategy classifier — primary consumer of new social features),
> Round 10 (causal inference — news events are instrumental variables,
> partial dependency already wired in R10's MVP).

---

## 1. The thesis — leaders telegraph

Many leaders on Polymarket are also publicly visible on Twitter/X,
Telegram, and Discord. They post their thesis, sometimes their entries,
sometimes their exits. **The posts often precede the trades by
minutes**.

A different subset of leaders run cross-venue strategies — they trade
the same event on Polymarket, Kalshi, Manifold, and PredictIt
simultaneously. Their positions on Kalshi are alpha-relevant to their
positions on Polymarket. Today we see ZERO of those positions.

> Round 12 ingests social signals (X / Telegram / Discord) and
> cross-market positions (Kalshi / Manifold / PredictIt) as new
> daemons in the Round 6 framework, feeds them into R8's strategy
> classifier as new feature dimensions, and surfaces them as
> instruments for R10's causal estimator.

This is the **periphery**: data that isn't directly tradeable, but
that conditions the interpretation of the leader's on-chain actions.

---

## 2. The architecture — two new daemons, the Periphery pair

```
systemd units (post-R12):
  polymarket-engine.service          # (existing)
  polymarket-observer.service        # (existing)
  polymarket-onchain.service         # (R6)
  polymarket-crawler.service         # (R6)
  polymarket-falcon-refresher.service # (R6)
  polymarket-mempool.service         # (R7)
  polymarket-book-l3.service         # (R11)
  polymarket-social.service          # (R12 — NEW)
  polymarket-crossmarket.service     # (R12 — NEW)
  polymarket-api.service             # (existing)
```

**Both new daemons fit in the CX23 envelope**:
- `polymarket-social.service`: ~250 MB (X stream + NLP classifier)
- `polymarket-crossmarket.service`: ~200 MB (3 venue clients + matcher)

Total daemons post-R12: 10. Combined memory: ~3.5 GB. CX23 has 4 GB.
Tight but operational. If we hit pressure, upgrade to CX33 (8 GB,
+€6/mo) — pre-budgeted contingency.

---

## 3. Component breakdown — Social

### 3.1 `src/social/x_firehose.py` — X (Twitter) filtered stream

```python
class XFirehoseSubscriber:
    """X API v2 filtered stream subscriber.

    Filter rules (POSTed to X API on startup):
      - from:<handle> for every known leader handle
      - URL filters for polymarket.com market URLs
      - Keyword filters for top market subjects
      - Combined OR via X's rules language

    Rate budget (X API basic tier, $100/mo):
      - 10K tweet pulls/month
      - With our filter rules, expect ~5K/month — well within budget

    Output: every matched tweet → Redis Stream `social:x:stream`
    with metadata:
      {
        author_handle, author_wallet (resolved if known),
        text, posted_at, market_urls, mentions, sentiment_seed,
        is_retweet, parent_id, ...
      }

    Fallback: if X API limit is hit, the stream pauses gracefully
    rather than crashing. Operator alert on `polybot_social_x_quota_remaining`
    < 10 %.
    """
```

### 3.2 `src/social/nlp_classifier.py` — Tweet intent classification

This is the only ML component of R12. A small fine-tuned classifier
that maps tweet text → {entry_signal, exit_signal, noise}.

```python
class TweetIntentClassifier:
    """Lightweight classifier on tweet text → 3-class output.

    Training: ~500 hand-labelled tweets from known-leader handles.
    Model: distilbert-base (small enough to run on CPU in 100ms),
    fine-tuned with class weights to handle imbalance (noise dominates).

    Why not GPT/Claude API:
      - Latency: 100ms local vs 1-2s API
      - Cost: free locally vs $0.001/call × 5K tweets/month = small but
              additive
      - Reproducibility: a fixed local model gives stable backtests
      - Privacy: no leak of our watched-leader list to a third party

    Output per tweet:
      {
        'intent': 'entry_signal' | 'exit_signal' | 'noise',
        'confidence': float,
        'parsed_market': market_id | None,
        'parsed_direction': 'yes' | 'no' | None,
      }
    """
```

### 3.3 `src/social/telegram_listener.py` and `src/social/discord_listener.py`

Public-channel readers for the channels we know leaders participate in.
Same output schema as X (after format normalization).

Public channels only — no DM access, no private group infiltration.
Operationally simpler and ethically cleaner.

### 3.4 Social feature derivation

The R8 strategy classifier consumes new per-wallet features:

```
H. SOCIAL (4 features, now wired by R12)
   - social_signal_density  (tweets/day matching this wallet's handle)
   - tweets_per_active_day
   - tweet_to_trade_lag_median_s  (signed: negative = tweet BEFORE trade)
   - social_signal_strategy_concordance  (NLP-classified intent vs
                                          actual trade direction)
```

A `social_to_trade_lag_median_s` near 0 with high
`social_signal_strategy_concordance` is the signature of the
"social_driven" strategy class.

---

## 4. Component breakdown — Cross-Market

### 4.1 Per-venue clients

```python
# src/cross_market/kalshi_client.py
class KalshiClient:
    """Kalshi API client. Authenticated read-only.

    Fetches:
      - market metadata (mapping event → market_id)
      - position snapshots per watched wallet (if Kalshi exposes;
        otherwise via on-chain proxies)
      - trade events via WebSocket where available

    Rate limit handling reuses the FalconClient adaptive token bucket
    pattern.
    """

# src/cross_market/manifold_client.py
class ManifoldClient:
    """Manifold Markets is play-money but has open API + open data.
    Excellent for discovery: traders on Manifold often appear on
    Polymarket later. Treat as a 'farm league' signal."""

# src/cross_market/predictit_client.py
class PredictItClient:
    """PredictIt is regulated and US-only. We can read public
    prices + per-market position aggregates but NOT individual
    positions. Still useful for cross-market pricing comparisons."""
```

### 4.2 `src/cross_market/wallet_resolver.py` — Cross-venue identity

The hardest part. Same trader uses different addresses on different
venues. Resolution sources:

1. **Public profile pages**: many leaders link multiple addresses
   on their X bio or Polymarket profile
2. **Twitter handle**: if same handle posts about positions on
   multiple venues, infer same operator
3. **Behavioral fingerprint**: trading patterns sometimes match
   across venues (R8 strategy classifier signatures, R11
   microstructure)

```python
class WalletResolver:
    """Maps Polymarket wallet → set[VenueAddress].

    Manual seed: operator hand-curates the top 100 mappings; that's
    enough to bootstrap.

    Automatic match: for each Polymarket wallet without a manual
    seed, fingerprint their strategy class + R11 microstructure
    signature + active hours, search Kalshi for matching wallets.
    Output a candidate match with confidence; operator confirms.

    The mapping is intentionally manual-in-the-loop. Auto-merging
    is a false-positive risk we don't take.
    """
```

### 4.3 `src/cross_market/position_aggregator.py`

```python
class CrossMarketPositionAggregator:
    """For each watched wallet, fetch positions across all resolved
    venues. Emit a unified position snapshot per resolved identity.

    Output to `cross_market_positions` table (migration 035):
      {
        operator_id (our internal ID for resolved identities),
        polymarket_wallet, kalshi_account, manifold_handle, ...,
        venue_positions: [
          {venue, market, side, size_usd, opened_at}
        ],
        snapshot_at
      }
    """
```

### 4.4 Cross-market features for R8

```
NEW: cross-market features (R12 wires)
   - active_venue_count  (how many venues does this operator trade?)
   - cross_venue_correlation  (do they go same direction on same event?)
   - cross_venue_lag_s  (Kalshi positions lead/lag Polymarket?)
```

Operators trading on multiple venues with high cross-venue correlation
get a new strategy class candidate: `cross_market_arb`. If R8's
unsupervised explorer (§ 3.4 of R8 spec) flags this cluster after R12
ships, we add it to the taxonomy.

---

## 5. Migration sequence

```sql
-- Migration 035
CREATE TABLE social_signals (
    signal_id BIGSERIAL PRIMARY KEY,
    source VARCHAR(20) NOT NULL,         -- x|telegram|discord
    author_handle VARCHAR(100) NOT NULL,
    resolved_wallet VARCHAR(100),         -- NULL if unresolved
    posted_at TIMESTAMPTZ NOT NULL,
    text TEXT NOT NULL,
    intent VARCHAR(20) NOT NULL,          -- entry_signal|exit_signal|noise
    intent_confidence NUMERIC(5, 4) NOT NULL,
    parsed_market VARCHAR(100),
    parsed_direction VARCHAR(4),
    raw_payload JSONB
);
CREATE INDEX idx_ss_author_time ON social_signals (author_handle, posted_at DESC);
CREATE INDEX idx_ss_wallet_time ON social_signals
    (resolved_wallet, posted_at DESC) WHERE resolved_wallet IS NOT NULL;

-- Migration 036
CREATE TABLE cross_market_operators (
    operator_id BIGSERIAL PRIMARY KEY,
    polymarket_wallet VARCHAR(100),
    kalshi_account VARCHAR(100),
    manifold_handle VARCHAR(100),
    predictit_account VARCHAR(100),
    x_handle VARCHAR(100),
    resolution_source VARCHAR(40) NOT NULL,  -- manual|fingerprint|profile_link
    confidence NUMERIC(5, 4) NOT NULL,
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes TEXT
);
CREATE INDEX idx_cmo_pm ON cross_market_operators (polymarket_wallet);
CREATE INDEX idx_cmo_kalshi ON cross_market_operators (kalshi_account);

-- Migration 037
CREATE TABLE cross_market_positions (
    snapshot_id BIGSERIAL PRIMARY KEY,
    operator_id BIGINT NOT NULL REFERENCES cross_market_operators(operator_id),
    venue VARCHAR(20) NOT NULL,
    market_id VARCHAR(200) NOT NULL,
    side VARCHAR(10) NOT NULL,
    size_usdc NUMERIC(20, 2) NOT NULL,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_cmp_operator_time ON cross_market_positions (operator_id, snapshot_at DESC);
CREATE INDEX idx_cmp_venue_market ON cross_market_positions (venue, market_id);
```

---

## 6. New Prometheus metrics (Round 12 contributes ~14)

```
polybot_social_tweets_ingested_total{source}     # x|telegram|discord
polybot_social_tweets_classified_total{intent}   # entry|exit|noise
polybot_social_x_quota_remaining                 # gauge of X API budget
polybot_social_classifier_latency_seconds        # NLP inference time
polybot_social_classifier_uncertainty            # avg max-prob across recent batch
polybot_social_unresolved_authors                # gauge: handles we can't map to wallets

polybot_crossmarket_venues_reachable             # gauge: how many of 3 venues are responsive
polybot_crossmarket_api_calls_total{venue, result}
polybot_crossmarket_api_latency_seconds{venue}
polybot_crossmarket_positions_observed_total{venue}
polybot_crossmarket_resolved_operators           # gauge: total resolved identities
polybot_crossmarket_resolution_attempts_total{source, result}

polybot_social_cross_market_signal_coverage       # gauge: fraction of top-N leaders with social OR cross-market data
polybot_social_to_trade_lag_seconds               # histogram across leaders
```

---

## 7. Effort, dependencies, risk

### Effort (single dev)

| Component | Weeks |
|---|---|
| X firehose subscriber + rule management | 0.75 |
| NLP classifier (data labelling + fine-tune) | 1.25 |
| Telegram + Discord listeners | 0.5 |
| Per-venue clients (Kalshi, Manifold, PredictIt) | 1.0 |
| Wallet resolver (seed + matcher framework) | 0.5 |
| Position aggregator | 0.5 |
| Migrations + tests + audit doc + R8 retrain | 0.5 |
| **Total** | **~5 weeks** (can run parallel with R10 / R11) |

### Dependencies

- Round 6 (daemon framework)
- Round 8 (R8 retrains with new features; the measurable acceptance
  criterion is R8 accuracy improvement)
- Round 10 (partial): R10's MVP already uses NewsAPI; R12 expands the
  event corpus

External dependencies:
- X API basic tier subscription ($100/mo)
- Kalshi API key (free; rate-limited)
- Manifold API key (free; public data)
- PredictIt API (public, no key)

### Risk: 3/5

| Risk | Severity | Mitigation |
|---|---|---|
| X signal-to-noise ratio brutal | High | Heavy per-leader allow-listing (only watched handles); NLP classifier filters noise class with high confidence threshold |
| Wallet resolution false-positives | High | Manual-in-the-loop; auto-match suggestions get operator confirmation; resolution_source documented |
| Venue API changes | Medium | Per-venue client with versioned schema; integration tests on real APIs in CI |
| Kalshi access (regulated venue) | Low | Read-only API; we're not trading, just observing public market data |
| Operator over-trusts social signal | Medium | Surfaces in R8 with confidence weighting; never a standalone trade trigger |

### Acceptance criteria

- X firehose ingests > 1000 tweets/month sustained
- NLP classifier validation accuracy ≥ 80 % on 100-tweet held-out set
- ≥ 20 leaders have ≥ 30 days of social signal coverage (the social
  feature is meaningful for them)
- ≥ 10 cross-market operators resolved (manual or auto-confirmed)
- After R8 retrain with R12 features, the `social_driven` strategy
  class becomes detectable (precision ≥ 0.7 on val set)

---

## 8. Rollout plan

### Phase 12.A — X firehose + NLP labelling sprint (weeks 1-2)
1. X API subscription, filter rules deployed
2. Operator hand-labels 500 tweets across known leader handles (5 hrs
   focused work)
3. Fine-tune distilbert; achieve target accuracy
4. **Gate**: classifier val accuracy ≥ 80 %, deployed in shadow

### Phase 12.B — Telegram/Discord + cross-market ingest (week 3)
1. Public TG/Discord listeners
2. Kalshi + Manifold + PredictIt clients with adaptive rate-limit handling
3. **Gate**: 7 days of clean ingestion across all sources

### Phase 12.C — Wallet resolution sprint (week 4)
1. Operator manual seeds (~100 mappings)
2. Auto-matcher framework (fingerprint-based suggestions)
3. **Gate**: ≥ 10 cross-market operators resolved with confidence ≥ 0.8

### Phase 12.D — Feature integration + R8 retrain (week 4.5)
1. Cross-market + social features added to R8 LeaderFeatureExtractor
2. R8 retrains, validates
3. **Gate**: R8 accuracy improves ≥ 3 pp OR `social_driven` class
   precision ≥ 0.7

---

## 9. What this round explicitly does NOT do

- **Does NOT trade based on social signal directly**. Social is a feature
  for R8 classification + R10 instrumental variable, not a trade trigger.
- **Does NOT scrape private channels or DMs**. Public only. Ethical
  and legal floor.
- **Does NOT do sentiment scoring** (e.g., "bullish" / "bearish"). The
  intent classifier (entry / exit / noise) is more useful and simpler
  than sentiment. Sentiment is a Round-13+ research question if it
  becomes relevant.
- **Does NOT integrate with Polymarket's own social features** (their
  market chat, etc.). Polymarket's chat is too noisy to be useful; the
  per-platform listeners give better signal.

---

## 10. The non-obvious gains

1. **Tweet-to-trade lag is a leader-quality signal**. Leaders with
   short positive lag (tweet shortly AFTER entering) are likely
   sharing genuine theses. Leaders with long negative lag (tweet
   BEFORE entering — telegraphing) are either highly confident OR
   trying to attract followers (in which case our R8 'social_driven'
   class catches them).

2. **Cross-market positions calibrate the IV estimator (R10)**.
   When a Polymarket leader holds the opposite side of the same event
   on Kalshi, we know they're hedging — their Polymarket position is
   NOT a directional view. This is an instrument: cross-venue
   hedging is exogenous to follower behavior.

3. **The X firehose is a free regime-change detector**. When tweet
   volume on a topic spikes 10x in an hour, something big happened.
   The bot can pause new entries on that topic until the news event
   resolves into the existing price.

4. **Discoverable new strategies via cross-market**. If we resolve
   cross-market operators and they trade on Polymarket BUT NOT
   correlated with Kalshi/Manifold positions, they're cross-market
   independent — a different strategy class than pure-Polymarket
   leaders. New R8 cluster.

---

## 11. The single sentence

> Round 12 ingests **what leaders say** (X, Telegram, Discord) and
> **what they trade elsewhere** (Kalshi, Manifold, PredictIt), wires
> both as new feature dimensions for R8 strategy classification + new
> instruments for R10 causal estimation — so we condition the bot's
> interpretation of every leader on-chain action on the off-chain
> context that surrounds it.
