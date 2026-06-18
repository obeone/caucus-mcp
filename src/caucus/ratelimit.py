"""A small, dependency-free token-bucket rate limiter.

Each sender gets one bucket. Beyond protecting the hub, the limiter doubles as
a brake on runaway agent-to-agent loops: when an agent floods, its ``send``
calls start failing, which naturally slows the exchange down.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class TokenBucket:
    """A classic token bucket.

    Attributes:
        capacity: Maximum number of tokens (burst size).
        refill_rate: Tokens added per second.
        tokens: Current token count.
        updated: Last refill timestamp.
    """

    capacity: float
    refill_rate: float
    tokens: float = field(default=0.0)
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.tokens == 0.0:
            self.tokens = self.capacity

    def allow(self, cost: float = 1.0) -> bool:
        """Consume ``cost`` tokens if available.

        Returns:
            ``True`` if the request is permitted, ``False`` if rate-limited.
        """
        now = time.monotonic()
        elapsed = now - self.updated
        self.updated = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def retry_after(self, cost: float = 1.0) -> float:
        """Estimate seconds until ``cost`` tokens will be available."""
        if self.tokens >= cost:
            return 0.0
        if self.refill_rate <= 0:
            return float("inf")
        return (cost - self.tokens) / self.refill_rate

    def available(self) -> float:
        """Return the tokens available right now without consuming any.

        Performs the same lazy refill as :meth:`allow` but read-only, so callers
        can probe a bucket's fill level (e.g. to evict fully-refilled, idle
        buckets from a per-key map) without disturbing its state.
        """
        now = time.monotonic()
        return min(self.capacity, self.tokens + (now - self.updated) * self.refill_rate)

    def reconfigure(self, *, capacity: float, refill_rate: float) -> None:
        """Retune this bucket in place to a new capacity and refill rate.

        Credits elapsed idle time under the *old* rate first (the same lazy
        refill as :meth:`allow`), then adopts the new parameters and clamps the
        live token count down to the new capacity. Tightening therefore takes
        effect immediately (tokens are clamped); loosening recovers gradually at
        the new rate.

        Mutating in place is deliberate. Reconstructing the bucket would re-run
        :meth:`__post_init__`, which seeds ``tokens`` to ``capacity`` whenever
        ``tokens == 0.0`` — silently handing the sender a full fresh burst on
        every retune. Always reconfigure; never replace.

        Args:
            capacity: New maximum token count (burst size).
            refill_rate: New tokens added per second.
        """
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_rate)
        self.updated = now
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = min(self.tokens, capacity)
