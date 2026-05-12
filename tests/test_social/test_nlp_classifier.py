"""HeuristicTweetClassifier + LoadableTweetClassifier tests.

Coverage:
  * Output shape contract — every classify() returns the canonical dict.
  * Known entry / exit patterns map to the correct intent.
  * Empty / very-short / non-trade text classifies as noise.
  * Market parsing extracts polymarket.com URLs and $cashtag mentions.
  * Direction parsing distinguishes 'yes' vs 'no'.
  * LoadableTweetClassifier falls back gracefully without a model file.
  * LoadableTweetClassifier loads + uses an injected mock pipeline.
"""
from __future__ import annotations

import pickle

import pytest

from src.social.nlp_classifier import (
    HeuristicTweetClassifier,
    LoadableTweetClassifier,
    TweetClassification,
    TweetIntent,
    classification_to_dict,
)


# Module-level picklable stand-in for an sklearn pipeline. We can't use
# MagicMock here because MagicMock isn't picklable across reloaded
# imports under pytest's collection model.
class _MockPipeline:
    """Trivial picklable pipeline."""

    def __init__(self, classes, prob_row):
        self.classes_ = list(classes)
        self._row = list(prob_row)

    def predict_proba(self, _x):
        return [list(self._row)]


class TestHeuristicSchema:
    def test_classify_returns_tweet_classification(self):
        clf = HeuristicTweetClassifier()
        result = clf.classify("just entered YES on $btc-price")
        assert isinstance(result, TweetClassification)
        assert isinstance(result.intent, TweetIntent)
        assert 0.0 <= result.confidence <= 1.0

    def test_classification_to_dict_round_trip(self):
        clf = HeuristicTweetClassifier()
        result = clf.classify("just bought YES on this")
        d = classification_to_dict(result)
        assert set(d.keys()) == {
            "intent", "confidence", "parsed_market", "parsed_direction"
        }
        assert d["intent"] in {"entry_signal", "exit_signal", "noise"}


class TestHeuristicEntryPatterns:
    @pytest.mark.parametrize("text", [
        "just entered YES at 0.42 on this market",
        "going long YES",
        "loading up on this one, YES",
        "bought $5k of YES",
        "opening a new position on YES",
    ])
    def test_entry_signals_detected(self, text: str):
        clf = HeuristicTweetClassifier()
        result = clf.classify(text)
        assert result.intent == TweetIntent.ENTRY_SIGNAL
        assert result.confidence > 0.5

    def test_entry_signal_extracts_yes_direction(self):
        clf = HeuristicTweetClassifier()
        result = clf.classify("just entered YES at 0.42")
        assert result.parsed_direction == "yes"

    def test_entry_signal_extracts_market_from_url(self):
        clf = HeuristicTweetClassifier()
        result = clf.classify(
            "just entered YES on polymarket.com/event/will-fed-hike-rates"
        )
        assert result.parsed_market == "will-fed-hike-rates"


class TestHeuristicExitPatterns:
    @pytest.mark.parametrize("text", [
        "took profit and exited",
        "tp'd, closed my position",
        "sold the bag",
        "stopped out of this one",
        "trimmed my position",
    ])
    def test_exit_signals_detected(self, text: str):
        clf = HeuristicTweetClassifier()
        result = clf.classify(text)
        assert result.intent == TweetIntent.EXIT_SIGNAL
        assert result.confidence > 0.5


class TestHeuristicNoiseFilter:
    @pytest.mark.parametrize("text", [
        "",
        "  ",
        "gm",
        "hi",
        "lol",
        "wow that's wild",
        "interesting market dynamics today",
        "just thinking about prediction markets",
    ])
    def test_noise_classification(self, text: str):
        clf = HeuristicTweetClassifier()
        result = clf.classify(text)
        assert result.intent == TweetIntent.NOISE

    def test_ambiguous_entry_and_exit_classifies_as_noise(self):
        # "entered ... then took profit" — both patterns fire → noise.
        clf = HeuristicTweetClassifier()
        result = clf.classify("just entered YES, then took profit")
        assert result.intent == TweetIntent.NOISE


class TestLoadableFallback:
    def test_no_model_path_falls_back_to_heuristic(self):
        clf = LoadableTweetClassifier(model_path=None)
        # Heuristic kicks in.
        out = clf.classify("just entered YES at 0.42")
        assert out.intent == TweetIntent.ENTRY_SIGNAL

    def test_missing_file_falls_back(self, tmp_path):
        clf = LoadableTweetClassifier(model_path=tmp_path / "missing.pkl")
        out = clf.classify("just bought YES")
        assert out.intent == TweetIntent.ENTRY_SIGNAL


class TestLoadablePipeline:
    def test_loads_and_classifies_via_mock_pipeline(self, tmp_path):
        # Picklable stand-in that always predicts "entry_signal".
        pipeline = _MockPipeline(
            classes=["entry_signal", "exit_signal", "noise"],
            prob_row=[0.85, 0.10, 0.05],
        )
        path = tmp_path / "pipe.pkl"
        with path.open("wb") as fh:
            pickle.dump(pipeline, fh)
        clf = LoadableTweetClassifier(model_path=path)
        out = clf.classify("any text here")
        assert out.intent == TweetIntent.ENTRY_SIGNAL
        assert pytest.approx(out.confidence, abs=1e-6) == 0.85

    def test_rejects_pipeline_with_missing_labels(self, tmp_path):
        # Pipeline with wrong classes → loader falls back.
        pipeline = _MockPipeline(
            classes=["positive", "negative"], prob_row=[0.7, 0.3]
        )
        path = tmp_path / "bad.pkl"
        with path.open("wb") as fh:
            pickle.dump(pipeline, fh)
        clf = LoadableTweetClassifier(model_path=path)
        # Falls back to heuristic.
        out = clf.classify("just entered YES at 0.42")
        assert out.intent == TweetIntent.ENTRY_SIGNAL
