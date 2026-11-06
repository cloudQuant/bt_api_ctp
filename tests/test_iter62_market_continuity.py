"""Iteration 62, D62-38: CTP tick continuity means current scope and no known loss.

User decision 2026-11-06 (D62-38): ``continuity_status`` says whether a tick
belongs to the current connection/subscription/trading-day scope without a
locally known message loss.  Volume accounting is a separate, bar-only fact.
The first tick of a scope is continuous (its volume is a baseline); a
cumulative-volume decrease only flags the volume; an out-of-order tick is
dropped; a one-sided book at the exchange limit price is a valid quote.

These tests drive the offline ``CtpMarketStream._on_tick`` seam (no native
client, no managed receipt).  Evidence level: UNIT/FAKE.
"""

from __future__ import annotations

from sys import float_info
from types import SimpleNamespace

import pytest

from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData
from bt_api_ctp.feeds.live_ctp_feed import CtpMarketStream, CtpVolumeDeltaTracker

DBL_MAX = float_info.max
UPPER = 4400.0
LOWER = 3600.0


def _fields(total: int, second: int, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "InstrumentID": "IF2506",
        "ExchangeID": "CFFEX",
        "LastPrice": 4000.0,
        "BidPrice1": 3999.0,
        "AskPrice1": 4001.0,
        "BidVolume1": 2,
        "AskVolume1": 3,
        "OpenInterest": 1000,
        "UpperLimitPrice": UPPER,
        "LowerLimitPrice": LOWER,
        "Volume": total,
        "TradingDay": "20260909",
        "ActionDay": "20260909",
        "UpdateTime": f"09:30:{second:02d}",
        "UpdateMillisec": 0,
    }
    fields.update(overrides)
    return fields


@pytest.fixture
def stream():
    stream = CtpMarketStream(topics=[])
    stream._md_client = SimpleNamespace(connection_generation=11)
    stream.rows = []
    stream.push_data = stream.rows.append
    return stream


def _push(stream: CtpMarketStream, total: int, second: int, **overrides: object) -> None:
    stream._on_tick(SimpleNamespace(**_fields(total, second, **overrides)))


def _volume(row: CtpTickerData) -> tuple[object, ...]:
    return (row.delta_volume, row.volume_complete, row.volume_quality, row.continuity_status)


def _flags(row: CtpTickerData) -> list[str]:
    return sorted(row.quality_flags)


# ------------------------------------------------------------ scope starts


def test_the_first_tick_of_a_stream_is_continuous_with_a_baseline_volume(stream) -> None:
    _push(stream, 100, 1)
    (row,) = stream.rows
    assert _volume(row) == (0.0, False, "BASELINE", "continuous")
    assert _flags(row) == []
    assert row.get_all_data()["execution_eligible"] is False  # never self-promoted


def test_the_first_tick_after_an_in_place_reconnect_is_continuous_with_a_baseline(
    stream,
) -> None:
    _push(stream, 100, 1)
    _push(stream, 105, 2)
    stream._md_client = SimpleNamespace(connection_generation=12)  # native reconnect
    _push(stream, 107, 3)
    _push(stream, 110, 4)
    first, counted, reconnected, resumed = stream.rows
    assert reconnected.connection_generation == first.connection_generation + 1
    assert reconnected.subscription_epoch == first.subscription_epoch  # no topics here
    # The new scope is visible in its generation; within it nothing is lost.
    assert _volume(reconnected) == (0.0, False, "CONNECTION_OR_TRADING_DAY_RESET", "continuous")
    assert _volume(resumed) == (3.0, True, "CONTINUOUS", "continuous")
    assert _volume(counted) == (5.0, True, "CONTINUOUS", "continuous")


def test_the_first_tick_of_a_new_trading_day_is_continuous_with_a_baseline(stream) -> None:
    _push(stream, 500, 1)
    _push(stream, 10, 2, TradingDay="20260910", ActionDay="20260909")
    _old_day, new_day = stream.rows
    assert new_day.trading_day == "20260910"
    assert _volume(new_day) == (0.0, False, "CONNECTION_OR_TRADING_DAY_RESET", "continuous")


def test_a_continuous_run_is_unchanged(stream) -> None:
    _push(stream, 100, 1)
    _push(stream, 104, 2)
    _push(stream, 104, 3)  # a quote-only change: zero delta, still complete
    _baseline, traded, quoted = stream.rows
    assert _volume(traded) == (4.0, True, "CONTINUOUS", "continuous")
    assert _volume(quoted) == (0.0, True, "CONTINUOUS", "continuous")
    assert _flags(traded) == [] and _flags(quoted) == []


# --------------------------------------------------------- volume defects


def test_a_cumulative_volume_decrease_keeps_continuity_and_flags_only_the_volume(
    stream,
) -> None:
    _push(stream, 100, 1)
    _push(stream, 90, 2)  # cumulative volume went down
    _push(stream, 96, 3)
    _baseline, reset, resumed = stream.rows
    assert _volume(reset) == (0.0, False, "VOLUME_RESET", "continuous")
    assert _flags(reset) == ["VOLUME_GAP"]
    # The tracker re-anchors on the reset row and keeps counting from it.
    assert _volume(resumed) == (6.0, True, "CONTINUOUS", "continuous")


# ------------------------------------------------------------ ordering


def test_an_out_of_order_tick_is_dropped_instead_of_marking_a_gap(stream) -> None:
    _push(stream, 100, 1)
    _push(stream, 104, 2)
    # The live callback numbers ticks under its lock, so this cannot happen on
    # one scope today; force an older ingest number to exercise the guard.
    stream._ingest_seq = 1
    _push(stream, 999, 3)
    _push(stream, 107, 4)
    assert [row.get_cumulative_volume() for row in stream.rows] == [100, 104, 107]
    _baseline, before, after = stream.rows
    assert _volume(after) == (3.0, True, "CONTINUOUS", "continuous")
    assert stream.quote_v2_out_of_order_dropped == 1


def test_the_tracker_keeps_an_out_of_order_row_out_of_its_state_and_never_continuous() -> None:
    tracker = CtpVolumeDeltaTracker()

    def tick(total: int, seq: int) -> CtpTickerData:
        return CtpTickerData(_fields(total, seq), connection_generation=1, ingest_seq=seq)

    tracker.apply(tick(100, 1))
    tracker.apply(tick(104, 2))
    late = tracker.apply(tick(999, 2))
    resumed = tracker.apply(tick(107, 3))
    assert late.volume_quality == "OUT_OF_ORDER" and "OUT_OF_ORDER" in late.quality_flags
    assert late.continuity_status != "continuous"
    assert _volume(resumed) == (3.0, True, "CONTINUOUS", "continuous")


# ------------------------------------------------------- quote validity


@pytest.mark.parametrize("empty_price", [DBL_MAX, 0.0])
def test_a_one_sided_book_bid_at_the_upper_limit_is_a_valid_quote(stream, empty_price) -> None:
    _push(stream, 100, 1)
    _push(
        stream, 108, 2,
        LastPrice=UPPER, BidPrice1=UPPER, BidVolume1=57, AskPrice1=empty_price, AskVolume1=0,
    )
    _push(stream, 110, 3, LastPrice=UPPER, BidPrice1=UPPER, AskPrice1=DBL_MAX, AskVolume1=0)
    _baseline, locked, still_locked = stream.rows
    assert _volume(locked) == (8.0, True, "CONTINUOUS", "continuous")
    assert _flags(locked) == ["LIMIT_LOCKED_UP", "ZERO_DEPTH"]
    assert _volume(still_locked) == (2.0, True, "CONTINUOUS", "continuous")


@pytest.mark.parametrize("empty_price", [DBL_MAX, 0.0])
def test_a_one_sided_book_ask_at_the_lower_limit_is_a_valid_quote(stream, empty_price) -> None:
    _push(
        stream, 100, 1,
        LastPrice=LOWER, AskPrice1=LOWER, AskVolume1=31, BidPrice1=empty_price, BidVolume1=0,
    )
    (locked,) = stream.rows
    assert _volume(locked) == (0.0, False, "BASELINE", "continuous")
    assert _flags(locked) == ["LIMIT_LOCKED_DOWN", "ZERO_DEPTH"]


@pytest.mark.parametrize(
    ("overrides", "flag"),
    [
        # The quoting side is near, but not exactly at, the limit price.
        ({"BidPrice1": UPPER - 0.2, "AskPrice1": DBL_MAX, "AskVolume1": 0}, "INVALID_ASK_PRICE"),
        ({"AskPrice1": LOWER + 0.2, "BidPrice1": DBL_MAX, "BidVolume1": 0}, "INVALID_BID_PRICE"),
        # The "empty" side still reports depth: not an empty side.
        ({"BidPrice1": UPPER, "AskPrice1": DBL_MAX, "AskVolume1": 4}, "INVALID_ASK_PRICE"),
        # Nobody is quoting the limit side.
        ({"BidPrice1": UPPER, "BidVolume1": 0, "AskPrice1": DBL_MAX, "AskVolume1": 0},
         "INVALID_ASK_PRICE"),
        # The bid sits at the lower limit with an empty ask: not a limit lock.
        ({"BidPrice1": LOWER, "AskPrice1": DBL_MAX, "AskVolume1": 0}, "INVALID_ASK_PRICE"),
        # The limit prices of the same snapshot are missing or unusable.
        ({"UpperLimitPrice": DBL_MAX, "BidPrice1": UPPER, "AskPrice1": DBL_MAX, "AskVolume1": 0},
         "INVALID_ASK_PRICE"),
        ({"UpperLimitPrice": None, "BidPrice1": UPPER, "AskPrice1": DBL_MAX, "AskVolume1": 0},
         "INVALID_ASK_PRICE"),
        ({"LowerLimitPrice": UPPER + 1, "BidPrice1": UPPER, "AskPrice1": DBL_MAX,
          "AskVolume1": 0}, "INVALID_ASK_PRICE"),
        # The last price lies outside the limits of its own snapshot.
        ({"LastPrice": UPPER + 1, "BidPrice1": UPPER, "AskPrice1": DBL_MAX, "AskVolume1": 0},
         "INVALID_ASK_PRICE"),
        # A NaN or infinite empty side is malformed, not empty.
        ({"BidPrice1": UPPER, "AskPrice1": float("nan"), "AskVolume1": 0}, "INVALID_ASK_PRICE"),
        ({"BidPrice1": UPPER, "AskPrice1": float("inf"), "AskVolume1": 0}, "INVALID_ASK_PRICE"),
        # Both sides empty.
        ({"BidPrice1": DBL_MAX, "BidVolume1": 0, "AskPrice1": DBL_MAX, "AskVolume1": 0},
         "INVALID_BID_PRICE"),
        # A limit-locked book that is malformed elsewhere is not marked locked.
        ({"BidPrice1": UPPER, "AskPrice1": DBL_MAX, "AskVolume1": 0, "OpenInterest": -1},
         "INVALID_OPEN_INTEREST"),
        # A crossed book away from the limits.
        ({"BidPrice1": 4002.0, "AskPrice1": 4001.0}, "CROSSED_BOOK"),
        # A crossed book that touches a limit is still crossed.
        ({"BidPrice1": UPPER, "AskPrice1": UPPER - 1}, "CROSSED_BOOK"),
        ({"LastPrice": 0.0}, "INVALID_LAST_PRICE"),
    ],
)
def test_genuinely_invalid_quotes_stay_invalid_gaps(stream, overrides, flag) -> None:
    _push(stream, 100, 1)
    _push(stream, 105, 2, **overrides)
    _push(stream, 110, 3)
    _baseline, invalid, after = stream.rows
    assert _volume(invalid) == (0.0, False, "INVALID_QUOTE", "gap")
    assert flag in invalid.quality_flags
    assert not {"LIMIT_LOCKED_UP", "LIMIT_LOCKED_DOWN"} & set(invalid.quality_flags)
    # An invalid row never moves the volume baseline.
    assert _volume(after) == (10.0, True, "CONTINUOUS", "continuous")


@pytest.mark.parametrize(
    ("overrides", "flag"),
    [
        ({"BidVolume1": 0}, "ZERO_DEPTH"),
        ({"BidPrice1": 4000.0, "AskPrice1": 4000.0}, "LOCKED_BOOK"),
    ],
)
def test_thin_or_locked_books_are_warnings_that_keep_continuity(stream, overrides, flag) -> None:
    _push(stream, 100, 1)
    _push(stream, 103, 2, **overrides)
    _baseline, warned = stream.rows
    assert _flags(warned) == [flag]
    assert _volume(warned) == (3.0, True, "CONTINUOUS", "continuous")


# ------------------------------------------------------ container defaults


def test_volume_facts_alone_never_decide_continuity() -> None:
    row = CtpTickerData(_fields(100, 1), connection_generation=1, ingest_seq=1)
    row.init_data()
    row.apply_volume_delta(0, complete=False, quality="BASELINE", continuity_status="continuous")
    assert row.continuity_status == "continuous"
    row = CtpTickerData(_fields(100, 1), connection_generation=1, ingest_seq=1)
    row.init_data()
    row.apply_volume_delta(5, complete=True, quality="CONTINUOUS", continuity_status="gap")
    assert row.continuity_status == "gap"
    # Anything outside the provider's two verdicts fails closed.
    row = CtpTickerData(_fields(100, 1), connection_generation=1, ingest_seq=1)
    row.init_data()
    row.apply_volume_delta(5, complete=True, quality="CONTINUOUS", continuity_status="snapshot")
    assert row.continuity_status == "gap"
