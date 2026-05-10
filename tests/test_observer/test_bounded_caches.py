"""
Unit tests for the bounded helpers used by TradeObserver.

These guards exist because the observer process is intended to run 24/7 on a
small Oracle Cloud Free VM; previously `_market_meta_cache` (dict) and
`_leader_condition_ids` (set) had no caps and would grow until the process
OOMed after enough new markets / leaders were seen.
"""

from __future__ import annotations

import time

import pytest

from src.observer.trade_observer import _BoundedSet, _BoundedTTLCache


# --------------------------------------------------------------------------- #
# _BoundedTTLCache                                                             #
# --------------------------------------------------------------------------- #


def test_ttl_cache_evicts_oldest_when_over_capacity():
    cache = _BoundedTTLCache(maxsize=3, ttl=60)
    for i in range(5):
        cache[f"k{i}"] = float(i)

    assert len(cache) == 3
    # Oldest two were evicted, newest three retained.
    assert "k0" not in cache
    assert "k1" not in cache
    assert "k2" in cache
    assert "k3" in cache
    assert "k4" in cache


def test_ttl_cache_get_refreshes_recency_so_lru_protects_hot_keys():
    cache = _BoundedTTLCache(maxsize=3, ttl=60)
    cache["a"] = 1.0
    cache["b"] = 2.0
    cache["c"] = 3.0
    # Touch "a" so it becomes most-recently-used; "b" is now LRU.
    assert cache.get("a") == 1.0
    cache["d"] = 4.0  # eviction → "b"

    assert "a" in cache
    assert "b" not in cache
    assert "c" in cache
    assert "d" in cache


def test_ttl_cache_get_returns_default_for_missing_or_expired():
    cache = _BoundedTTLCache(maxsize=10, ttl=0.05)
    cache["x"] = 1.0
    assert cache.get("x", 0.0) == 1.0
    assert cache.get("missing", 0.0) == 0.0

    time.sleep(0.07)  # expire
    assert cache.get("x", 0.0) == 0.0
    # Expired key was purged on access.
    assert len(cache) == 0


def test_ttl_cache_setitem_resets_expiry():
    cache = _BoundedTTLCache(maxsize=10, ttl=0.05)
    cache["x"] = 1.0
    time.sleep(0.03)
    cache["x"] = 2.0  # rewrite resets ttl
    time.sleep(0.03)
    # Total elapsed = 0.06s, but expiry was reset at 0.03s, so still alive.
    assert cache.get("x", 0.0) == 2.0


def test_ttl_cache_zero_or_negative_maxsize_floors_to_one():
    cache = _BoundedTTLCache(maxsize=0, ttl=60)
    cache["a"] = 1.0
    cache["b"] = 2.0
    assert len(cache) == 1
    assert "b" in cache and "a" not in cache


def test_ttl_cache_getitem_raises_keyerror_for_missing():
    cache = _BoundedTTLCache(maxsize=3, ttl=60)
    with pytest.raises(KeyError):
        _ = cache["nope"]


# --------------------------------------------------------------------------- #
# _BoundedSet                                                                  #
# --------------------------------------------------------------------------- #


def test_bounded_set_caps_size_with_fifo_eviction():
    s = _BoundedSet(maxsize=3)
    for i in range(5):
        s.add(f"m{i}")
    assert len(s) == 3
    assert "m0" not in s
    assert "m1" not in s
    assert "m2" in s
    assert "m3" in s
    assert "m4" in s


def test_bounded_set_add_is_idempotent_and_preserves_order():
    s = _BoundedSet(maxsize=3)
    s.add("a")
    s.add("b")
    s.add("c")
    s.add("a")  # already present; must not push out "b"
    s.add("d")  # this should evict the genuinely-oldest, "b"

    assert "a" in s  # protected — was inserted first but re-added kept it logically present
    assert "b" not in s
    assert "c" in s
    assert "d" in s


def test_bounded_set_update_respects_cap():
    s = _BoundedSet(maxsize=2)
    s.update(["a", "b", "c", "d"])
    assert len(s) == 2
    assert list(s)[-1] == "d"


def test_bounded_set_replace_clears_then_repopulates():
    s = _BoundedSet(maxsize=10, initial=["old1", "old2"])
    assert "old1" in s

    s.replace(["new1", "new2", "new3"])
    assert "old1" not in s
    assert "old2" not in s
    assert {"new1", "new2", "new3"} == set(s)


def test_bounded_set_truthiness_and_iteration():
    s = _BoundedSet(maxsize=5)
    assert not s
    s.add("x")
    assert s
    assert list(s) == ["x"]


def test_bounded_set_set_conversion_works_for_callers():
    """
    `_get_recent_leader_market_ids` returns `set(self._leader_condition_ids)`
    to its callers — make sure that conversion still works after we swapped
    from a real `set` to `_BoundedSet`.
    """
    s = _BoundedSet(maxsize=10, initial=["a", "b", "c"])
    assert set(s) == {"a", "b", "c"}
