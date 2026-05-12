"""Round 12 (The Periphery) — social signal ingest + NLP classification.

Public re-exports keep tests and callers from caring about the internal
module layout. New imports added to this list MUST stay below 500 lines'
worth of churn — the daemon shape is the load-bearing contract.
"""

from src.social.nlp_classifier import (
    HeuristicTweetClassifier,
    LoadableTweetClassifier,
    TweetIntent,
    TweetIntentClassifier,
    classification_to_dict,
)

__all__ = [
    "HeuristicTweetClassifier",
    "LoadableTweetClassifier",
    "TweetIntent",
    "TweetIntentClassifier",
    "classification_to_dict",
]
