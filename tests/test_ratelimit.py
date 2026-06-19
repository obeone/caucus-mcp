"""Unit tests for the :class:`~caucus.ratelimit.TokenBucket`.

Time is pinned through a fake ``monotonic`` clock so refill behaviour is
deterministic rather than wall-clock dependent.
"""

from __future__ import annotations

import math

import pytest

from caucus import ratelimit
from caucus.ratelimit import TokenBucket


class _Clock:
    """A controllable stand-in for ``time.monotonic``."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_post_init_fills_to_capacity_when_unset() -> None:
    bucket = TokenBucket(capacity=5.0, refill_rate=1.0)
    assert bucket.tokens == 5.0


def test_allow_consumes_until_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(100.0)
    monkeypatch.setattr(ratelimit.time, "monotonic", clock)
    bucket = TokenBucket(capacity=2.0, refill_rate=1.0, tokens=2.0, updated=100.0)

    assert bucket.allow() is True
    assert bucket.allow() is True
    assert bucket.allow() is False  # bucket drained, no time has passed


def test_allow_refills_over_time(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(100.0)
    monkeypatch.setattr(ratelimit.time, "monotonic", clock)
    bucket = TokenBucket(capacity=2.0, refill_rate=1.0, tokens=2.0, updated=100.0)

    assert bucket.allow() and bucket.allow()
    assert bucket.allow() is False

    clock.now = 101.0  # one second -> one token back
    assert bucket.allow() is True
    assert bucket.allow() is False


def test_refill_is_capped_at_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(100.0)
    monkeypatch.setattr(ratelimit.time, "monotonic", clock)
    bucket = TokenBucket(capacity=2.0, refill_rate=1.0, tokens=2.0, updated=100.0)

    clock.now = 1_000.0  # an age passes, but capacity is the ceiling
    assert bucket.allow() and bucket.allow()
    assert bucket.allow() is False


def test_retry_after_zero_when_tokens_available() -> None:
    bucket = TokenBucket(capacity=5.0, refill_rate=1.0)
    assert bucket.retry_after() == 0.0


def test_retry_after_estimates_from_deficit() -> None:
    bucket = TokenBucket(capacity=3.0, refill_rate=2.0)
    bucket.tokens = 0.5  # retry_after never advances the clock
    assert bucket.retry_after(cost=1.0) == pytest.approx(0.25)


def test_retry_after_infinite_without_refill() -> None:
    bucket = TokenBucket(capacity=3.0, refill_rate=0.0)
    assert bucket.allow() and bucket.allow() and bucket.allow()
    assert bucket.allow() is False
    assert math.isinf(bucket.retry_after())


def test_reconfigure_tightening_clamps_tokens_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(100.0)
    monkeypatch.setattr(ratelimit.time, "monotonic", clock)
    bucket = TokenBucket(capacity=10.0, refill_rate=1.0, tokens=10.0, updated=100.0)

    bucket.reconfigure(capacity=2.0, refill_rate=0.5)

    assert bucket.capacity == 2.0
    assert bucket.refill_rate == 0.5
    # No time has passed, so the full 10 tokens are clamped straight down to the
    # new, smaller capacity — tightening bites at once.
    assert bucket.tokens == 2.0


def test_reconfigure_loosening_does_not_reseed_to_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(100.0)
    monkeypatch.setattr(ratelimit.time, "monotonic", clock)
    bucket = TokenBucket(capacity=5.0, refill_rate=1.0, tokens=5.0, updated=100.0)

    # Drain the bucket dry (clock is static, so no refill credits in between).
    assert all(bucket.allow() for _ in range(5))
    assert bucket.allow() is False

    bucket.reconfigure(capacity=20.0, refill_rate=10.0)  # loosen hard

    # The reconstruction trap: a fresh TokenBucket would have reseeded tokens to
    # the new capacity (20). Reconfigure must leave the drained count intact (it
    # only refills by elapsed*old_rate, which is 0 here) so loosening recovers
    # gradually rather than handing out a free 20-message burst.
    assert bucket.capacity == 20.0
    assert bucket.tokens < 1.0


def test_reconfigure_credits_elapsed_under_old_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(100.0)
    monkeypatch.setattr(ratelimit.time, "monotonic", clock)
    bucket = TokenBucket(capacity=10.0, refill_rate=2.0, tokens=2.0, updated=100.0)

    clock.now = 103.0  # 3s elapsed -> +6 tokens at the OLD rate of 2/s -> 8
    bucket.reconfigure(capacity=10.0, refill_rate=1.0)

    # Elapsed idle time is credited under the old rate before the switch, then
    # clamped to the (unchanged) capacity.
    assert bucket.tokens == pytest.approx(8.0)
    assert bucket.refill_rate == 1.0
