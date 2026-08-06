import asyncio

import pytest

from luna_kb.runtime_controls import ConversationStore, MessageGate, QueueFull, WorkLimiter


def test_conversation_history_keeps_only_the_latest_three_turns() -> None:
    store = ConversationStore(max_turns=3, ttl_seconds=1800)
    key = (10001, 20001)

    for number in range(1, 5):
        store.append(key, f"问题{number}", f"回答{number}")

    assert store.get(key) == [
        {"role": "user", "content": "问题2"},
        {"role": "assistant", "content": "回答2"},
        {"role": "user", "content": "问题3"},
        {"role": "assistant", "content": "回答3"},
        {"role": "user", "content": "问题4"},
        {"role": "assistant", "content": "回答4"},
    ]


def test_conversation_history_expires_after_thirty_minutes() -> None:
    now = 0.0
    store = ConversationStore(
        max_turns=3,
        ttl_seconds=1800,
        clock=lambda: now,
    )
    key = (10001, 20001)
    store.append(key, "奖学金怎么申请", "以当年通知为准。")

    now = 1801.0

    assert store.get(key) == []


def test_conversation_history_evicts_old_users_at_global_capacity() -> None:
    now = 0.0
    store = ConversationStore(
        max_turns=1,
        ttl_seconds=1800,
        clock=lambda: now,
        max_conversations=2,
    )
    store.append((1, 1), "q1", "a1")
    now = 1.0
    store.append((1, 2), "q2", "a2")
    now = 2.0
    store.append((1, 3), "q3", "a3")

    assert store.get((1, 1)) == []
    assert store.get((1, 2))
    assert store.get((1, 3))


def test_duplicate_message_is_rejected_for_ten_minutes() -> None:
    now = 0.0
    gate = MessageGate(
        dedupe_seconds=600,
        cooldown_seconds=3,
        clock=lambda: now,
    )

    first = gate.admit("message-1", (10001, 20001))
    now = 599.0
    duplicate = gate.admit("message-1", (10001, 20001))

    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.reason == "duplicate_message"


def test_same_user_is_cooled_down_for_three_seconds() -> None:
    now = 0.0
    gate = MessageGate(
        dedupe_seconds=600,
        cooldown_seconds=3,
        clock=lambda: now,
    )
    key = (10001, 20001)
    assert gate.admit("message-1", key).accepted is True

    now = 2.9
    decision = gate.admit("message-2", key)

    assert decision.accepted is False
    assert decision.reason == "user_cooldown"


def test_message_dedupe_table_is_bounded() -> None:
    gate = MessageGate(
        dedupe_seconds=600,
        cooldown_seconds=0,
        max_entries=2,
    )

    assert gate.admit("message-1", (1, 1)).accepted is True
    assert gate.admit("message-2", (1, 2)).accepted is True
    assert gate.admit("message-3", (1, 3)).accepted is True

    # The oldest entry is evicted once the fixed memory bound is reached.
    assert gate.admit("message-1", (1, 1)).accepted is True


@pytest.mark.asyncio
async def test_work_limiter_rejects_messages_beyond_the_queue_capacity() -> None:
    limiter = WorkLimiter(concurrency=1, max_queue=1)
    first_entered = asyncio.Event()
    release = asyncio.Event()

    async def occupy_slot() -> None:
        async with limiter.slot():
            first_entered.set()
            await release.wait()

    first = asyncio.create_task(occupy_slot())
    await first_entered.wait()
    second = asyncio.create_task(occupy_slot())
    await asyncio.sleep(0)

    with pytest.raises(QueueFull):
        async with limiter.slot():
            pass

    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_work_limiter_expires_a_stale_queue_waiter() -> None:
    limiter = WorkLimiter(concurrency=1, max_queue=1, acquire_timeout_seconds=0.01)
    first_entered = asyncio.Event()
    release = asyncio.Event()

    async def occupy_slot() -> None:
        async with limiter.slot():
            first_entered.set()
            await release.wait()

    first = asyncio.create_task(occupy_slot())
    await first_entered.wait()
    try:
        with pytest.raises(QueueFull, match="timed out"):
            async with limiter.slot():
                pass
    finally:
        release.set()
        await first
