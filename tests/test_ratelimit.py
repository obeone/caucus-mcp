"""Unit tests for the :class:`~warroom.ratelimit.TokenBucket`.

Time is pinned through a fake ``monotonic`` clock so refill behaviour is
deterministic rather than wall-clock dependent.
"""

from __future__ import annotations

import math

import pytest

from warroom import ratelimit
from warroom.ratelimit import TokenBucket


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
