from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


ConversationKey = tuple[int, int]


class QueueFull(Exception):
    """The bounded answer queue has no remaining capacity."""


class WorkLimiter:
    def __init__(
        self,
        concurrency: int,
        max_queue: int,
        acquire_timeout_seconds: float | None = None,
    ) -> None:
        if concurrency <= 0 or max_queue < 0:
            raise ValueError("concurrency must be positive and max_queue cannot be negative")
        if acquire_timeout_seconds is not None and acquire_timeout_seconds <= 0:
            raise ValueError("acquire timeout must be positive")
        self._semaphore = asyncio.Semaphore(concurrency)
        self._capacity = concurrency + max_queue
        self._pending = 0
        self._acquire_timeout_seconds = acquire_timeout_seconds

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._pending >= self._capacity:
            raise QueueFull("answer queue is full")
        self._pending += 1
        acquired = False
        try:
            try:
                if self._acquire_timeout_seconds is None:
                    await self._semaphore.acquire()
                else:
                    await asyncio.wait_for(
                        self._semaphore.acquire(), self._acquire_timeout_seconds
                    )
            except TimeoutError as exc:
                raise QueueFull("answer queue wait timed out") from exc
            acquired = True
            yield
        finally:
            if acquired:
                self._semaphore.release()
            self._pending -= 1


@dataclass(frozen=True, slots=True)
class GateDecision:
    accepted: bool
    reason: str


class MessageGate:
    def __init__(
        self,
        dedupe_seconds: float,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 4096,
    ) -> None:
        if dedupe_seconds <= 0 or cooldown_seconds < 0 or max_entries <= 0:
            raise ValueError("message gate limits are invalid")
        self.dedupe_seconds = dedupe_seconds
        self.cooldown_seconds = cooldown_seconds
        self.max_entries = max_entries
        self.clock = clock
        self._message_times: dict[str, float] = {}
        self._message_order: deque[tuple[float, str]] = deque()
        self._user_times: dict[ConversationKey, float] = {}
        self._user_order: deque[tuple[float, ConversationKey]] = deque()

    def admit(self, message_id: str, key: ConversationKey) -> GateDecision:
        now = self.clock()
        self._expire(now)
        seen_at = self._message_times.get(message_id)
        if seen_at is not None and now - seen_at < self.dedupe_seconds:
            return GateDecision(False, "duplicate_message")
        admitted_at = self._user_times.get(key)
        if admitted_at is not None and now - admitted_at < self.cooldown_seconds:
            return GateDecision(False, "user_cooldown")
        self._message_times[message_id] = now
        self._message_order.append((now, message_id))
        while len(self._message_times) > self.max_entries:
            oldest_timestamp, oldest_message_id = self._message_order.popleft()
            if self._message_times.get(oldest_message_id) == oldest_timestamp:
                self._message_times.pop(oldest_message_id, None)
        self._user_times[key] = now
        self._user_order.append((now, key))
        return GateDecision(True, "accepted")

    def _expire(self, now: float) -> None:
        while (
            self._message_order
            and now - self._message_order[0][0] >= self.dedupe_seconds
        ):
            timestamp, message_id = self._message_order.popleft()
            if self._message_times.get(message_id) == timestamp:
                self._message_times.pop(message_id, None)
        while (
            self._user_order
            and now - self._user_order[0][0] >= self.cooldown_seconds
        ):
            timestamp, key = self._user_order.popleft()
            if self._user_times.get(key) == timestamp:
                self._user_times.pop(key, None)


class ConversationStore:
    def __init__(
        self,
        max_turns: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_conversations: int = 2048,
    ) -> None:
        if max_turns <= 0 or ttl_seconds <= 0 or max_conversations <= 0:
            raise ValueError("conversation limits must be positive")
        self.max_messages = max_turns * 2
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.max_conversations = max_conversations
        self._messages: dict[ConversationKey, deque[dict[str, str]]] = {}
        self._last_activity: dict[ConversationKey, float] = {}
        self._activity_order: deque[tuple[float, ConversationKey]] = deque()

    def append(self, key: ConversationKey, question: str, answer: str) -> None:
        now = self.clock()
        self._prune(now)
        messages = self._messages.setdefault(key, deque(maxlen=self.max_messages))
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})
        self._last_activity[key] = now
        self._activity_order.append((now, key))
        self._prune(now)

    def get(self, key: ConversationKey) -> list[dict[str, str]]:
        self._prune(self.clock())
        return list(self._messages.get(key, ()))

    def _prune(self, now: float) -> None:
        while self._activity_order:
            timestamp, key = self._activity_order[0]
            expired = now - timestamp >= self.ttl_seconds
            over_capacity = len(self._messages) > self.max_conversations
            if not expired and not over_capacity:
                break
            self._activity_order.popleft()
            if self._last_activity.get(key) != timestamp:
                continue
            self._last_activity.pop(key, None)
            self._messages.pop(key, None)
