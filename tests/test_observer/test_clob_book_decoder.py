"""Unit tests for :mod:`src.observer.clob_book_decoder` — Sprint 3 real-wire
fan-out decoder.

These cover the Polymarket Market channel wire shapes captured live from
prod (May 2026):

  * ``book``             — full L2 snapshot, no delta. Emits nothing.
  * ``price_change``     — N level updates. Emits ``placed`` for size
                            increases, ``cancelled`` for decreases /
                            level clears.
  * ``last_trade_price`` — a real fill. Emits one ``filled``.

The legacy single-event shapes (``order_placed`` / ``order_filled`` /
etc.) keep working via :func:`decode_ws_message` — we cover one as a
backward-compat smoke test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.observer.clob_book_decoder import (
    EVENT_CANCELLED,
    EVENT_FILLED,
    EVENT_PLACED,
    BookEvent,
    decode_ws_message,
    decode_ws_messages,
    is_known_non_event_message,
)


# --------------------------------------------------------------------------- #
# Fixtures captured live from the WS Market channel                            #
# --------------------------------------------------------------------------- #

# Real ``book`` snapshot (trimmed for readability — full payload tested
# functionally, not byte-for-byte). The decoder only cares about
# event_type; bids/asks contents are irrelevant.
_REAL_BOOK_PAYLOAD: dict = {
    "market": "0x9d3f02264a94bafc676afd7add8b11442e6ec72dabaa69cefef835f0672275c7",
    "asset_id": "25525886838936661349801315808447476243176190100445157889430252006398510133975",
    "timestamp": "1778627253438",
    "hash": "5c4bab19035b513c7336de2c9d9598f425a5948e",
    "bids": [{"price": "0.01", "size": "15913.28"}],
    "asks": [{"price": "0.99", "size": "12259.87"}],
    "event_type": "book",
}

# Real ``price_change`` shape: N changes packed into one WS frame.
_REAL_PRICE_CHANGE_PAYLOAD: dict = {
    "market": "0xc9f219a03f869a05d6329b7c38b0372fe93dda4308b29f76478fce7d44a1c32a",
    "price_changes": [
        {
            "asset_id": "70091137925865316384835856691538206349529130907106824964644903006047067942813",
            "price": "0.305",
            "size": "0",  # level cleared — cancel
            "side": "BUY",
            "hash": "2021d2bc67f1cea2bd36a9703564f7dfa8f36c72",
            "best_bid": "0.322",
            "best_ask": "0.329",
        },
        {
            "asset_id": "64497951991914880211975952060388782531642759324877922345815758665082418004678",
            "price": "0.695",
            "size": "150",  # 150 resting at this level now (was 100)
            "side": "SELL",
            "hash": "586d92c374f77fa64ee25e2c43d9a9a9f077665b",
        },
    ],
    "timestamp": "1778627269611",
    "event_type": "price_change",
}

# Real ``last_trade_price`` shape — an executed fill.
_REAL_LAST_TRADE_PAYLOAD: dict = {
    "market": "0xa4ddc18895cc7b14810283ef8f113939abffd3969c6a0e37f1897110c67e6f73",
    "asset_id": "51508280778202349361616850684455231843716212176724253736363122559269229712002",
    "price": "0.087",
    "size": "108.620688",
    "fee_rate_bps": "0",
    "side": "BUY",
    "timestamp": "1778627303972",
    "event_type": "last_trade_price",
    "transaction_hash": "0x6c80143635877687c82be050f883c2e1af0b9b278b34d9e28fb00d7c5952a49b",
}


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


class TestBookSnapshot:
    def test_book_snapshot_emits_no_events(self):
        """``book`` is a full L2 snapshot — there is no delta to emit.
        The decoder must return ``[]`` rather than synthesising fake
        events from every bid/ask level.
        """
        events = decode_ws_messages(_REAL_BOOK_PAYLOAD)
        assert events == []

    def test_book_snapshot_is_known_non_event(self):
        """The observer relies on this discriminator to not count book
        snapshots under the ``invalid`` drop label.
        """
        assert is_known_non_event_message(_REAL_BOOK_PAYLOAD) is True


class TestPriceChangePositive:
    def test_size_increase_emits_placed_with_positive_delta(self):
        """When the new resting size at a price level is larger than
        the previously cached size, the decoder must emit a ``placed``
        event with a positive ``size_delta``.
        """
        token_id = (
            "64497951991914880211975952060388782531642759324877922345815758665082418004678"
        )
        level_state: dict = {(token_id, "0.695", "sell"): Decimal("100")}
        # We isolate the positive change by reusing the real payload but
        # forcing only one change entry so the cache key matches.
        msg = dict(_REAL_PRICE_CHANGE_PAYLOAD)
        msg["price_changes"] = [_REAL_PRICE_CHANGE_PAYLOAD["price_changes"][1]]
        events = decode_ws_messages(msg, level_state=level_state)
        assert len(events) == 1
        evt = events[0]
        assert evt.event_type == EVENT_PLACED
        assert evt.side == "sell"
        assert evt.price == Decimal("0.695")
        assert evt.size_delta == Decimal("50")  # 150 new - 100 prev
        assert evt.wallet_address is None  # no wallet on non-fill
        # The level_state must reflect the NEW resting size after decode.
        assert level_state[(token_id, "0.695", "sell")] == Decimal("150")


class TestPriceChangeNegative:
    def test_level_cleared_emits_cancelled_with_negative_delta(self):
        """``size: "0"`` means the level was cleared. With a cache that
        knew about prior size N, the decoder must emit ``cancelled``
        with ``size_delta = -N``.
        """
        token_id = (
            "70091137925865316384835856691538206349529130907106824964644903006047067942813"
        )
        level_state: dict = {(token_id, "0.305", "buy"): Decimal("75")}
        msg = dict(_REAL_PRICE_CHANGE_PAYLOAD)
        msg["price_changes"] = [_REAL_PRICE_CHANGE_PAYLOAD["price_changes"][0]]
        events = decode_ws_messages(msg, level_state=level_state)
        assert len(events) == 1
        evt = events[0]
        assert evt.event_type == EVENT_CANCELLED
        assert evt.side == "buy"
        assert evt.price == Decimal("0.305")
        assert evt.size_delta == Decimal("-75")
        # Level state cleared.
        assert level_state[(token_id, "0.305", "buy")] == Decimal("0")

    def test_price_change_full_batch_fan_out(self):
        """The real wire frame packs both a clear and a positive change
        into one ``price_changes`` array — verify the decoder fans them
        out into two distinct BookEvents in order.
        """
        token_clear = (
            "70091137925865316384835856691538206349529130907106824964644903006047067942813"
        )
        token_grow = (
            "64497951991914880211975952060388782531642759324877922345815758665082418004678"
        )
        level_state: dict = {
            (token_clear, "0.305", "buy"): Decimal("75"),
            (token_grow, "0.695", "sell"): Decimal("100"),
        }
        events = decode_ws_messages(_REAL_PRICE_CHANGE_PAYLOAD, level_state=level_state)
        assert len(events) == 2
        # Order is preserved — first change in the array decodes first.
        assert events[0].token_id == token_clear
        assert events[0].event_type == EVENT_CANCELLED
        assert events[1].token_id == token_grow
        assert events[1].event_type == EVENT_PLACED


class TestLastTradePrice:
    def test_last_trade_price_emits_filled(self):
        """A real fill on the Market channel must map to ``filled``
        with the trade price, size, and side. Wallet stays NULL (spec
        § 3.1 — Market channel doesn't ship wallets).
        """
        events = decode_ws_messages(_REAL_LAST_TRADE_PAYLOAD)
        assert len(events) == 1
        evt = events[0]
        assert evt.event_type == EVENT_FILLED
        assert evt.side == "buy"
        assert evt.price == Decimal("0.087")
        assert evt.size_delta == Decimal("108.620688")
        assert evt.wallet_address is None  # spec § 3.1
        # The transaction_hash becomes the order_hash so the on-chain
        # reconciler can later join on it.
        assert evt.order_hash == (
            "0x6c80143635877687c82be050f883c2e1af0b9b278b34d9e28fb00d7c5952a49b"
        )


class TestLegacyBackwardCompat:
    def test_legacy_order_placed_still_works_via_fan_out(self):
        """The legacy single-event shape must keep working through the
        new fan-out entry point so existing producers don't break.
        """
        msg = {
            "event_type": "order_placed",
            "market_id": "m1",
            "token_id": "t1",
            "side": "buy",
            "price": "0.50",
            "size_delta": "100",
            "order_hash": "0xfeed",
            "timestamp": 1715500800,
        }
        events = decode_ws_messages(msg)
        assert len(events) == 1
        evt = events[0]
        assert isinstance(evt, BookEvent)
        assert evt.event_type == EVENT_PLACED
        assert evt.size_delta == Decimal("100")
        # Same payload through the legacy entry point must still work.
        legacy_evt = decode_ws_message(msg)
        assert legacy_evt is not None
        assert legacy_evt.event_type == EVENT_PLACED


class TestMalformed:
    def test_malformed_payload_emits_no_events(self):
        """Junk payloads return an empty list. The caller (observer)
        distinguishes them from valid-no-event frames via
        :func:`is_known_non_event_message`, which returns False here.
        """
        assert decode_ws_messages({"junk": True}) == []
        assert decode_ws_messages({"event_type": "weird_unknown_thing"}) == []
        # Non-dict input is also tolerated.
        assert decode_ws_messages("not a dict") == []  # type: ignore[arg-type]
        # And the discriminator must say "this is NOT a valid non-event".
        assert is_known_non_event_message({"junk": True}) is False
        assert is_known_non_event_message({"event_type": "weird"}) is False

    def test_price_change_missing_fields_skips_entries(self):
        """Entries with missing price/size/side must be skipped, but
        valid sibling entries in the same frame must still be emitted.
        """
        token_ok = (
            "64497951991914880211975952060388782531642759324877922345815758665082418004678"
        )
        msg = {
            "market": "0xabc",
            "event_type": "price_change",
            "timestamp": "1778627269611",
            "price_changes": [
                {"asset_id": token_ok, "price": None, "size": "10", "side": "BUY"},
                {"asset_id": token_ok, "price": "0.4", "size": "10", "side": "lolwut"},
                {"asset_id": token_ok, "price": "0.5", "size": "10", "side": "SELL"},
            ],
        }
        events = decode_ws_messages(msg, level_state={})
        assert len(events) == 1
        assert events[0].price == Decimal("0.5")
        assert events[0].side == "sell"


@pytest.mark.parametrize(
    "event_type, expected",
    [
        ("book", True),
        ("best_bid_ask", True),
        ("new_market", True),
        ("tick_size_change", True),
        ("market_resolved", True),
        ("price_change", False),
        ("last_trade_price", False),
        ("order_placed", False),
        ("weird_thing", False),
    ],
)
def test_is_known_non_event_message_matrix(event_type, expected):
    """The discriminator is the contract between decoder and observer
    for "valid but no delta". Lock it down with a parametrised matrix
    so a future event_type rename doesn't silently bump the invalid
    drop counter in prod.
    """
    assert is_known_non_event_message({"event_type": event_type}) is expected
