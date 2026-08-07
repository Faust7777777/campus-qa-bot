from __future__ import annotations

import asyncio
import contextlib
import os

from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment

from .application import CampusQaApplication
from .config import Settings
from .policy import InboundMessage
from .service import Runtime, tune_oom_score

driver = get_driver()
matcher = on_message(priority=20, block=False)
runtime: Runtime | None = None
application: CampusQaApplication | None = None
retry_task: asyncio.Task[None] | None = None


async def _connect_runtime(settings: Settings) -> bool:
    global runtime
    try:
        candidate = await Runtime.start(settings)
    except Exception:
        logger.exception("campus QA runtime failed to start; serving explicit startup errors")
        return False
    runtime = candidate
    if application is not None:
        application.answers = candidate.answers
    return True


async def _retry_runtime(settings: Settings) -> None:
    while runtime is None:
        await asyncio.sleep(60)
        if await _connect_runtime(settings):
            logger.info("campus QA runtime recovered after startup failure")
            return


@driver.on_startup
async def _startup() -> None:
    global application, retry_task
    access_token = os.getenv("ONEBOT_V11_ACCESS_TOKEN", "").strip()
    if not access_token or access_token.lower() in {"replace-me", "change-me"}:
        logger.error(
            "ONEBOT_V11_ACCESS_TOKEN is required; refusing to receive unauthenticated events"
        )
        return
    try:
        settings = Settings.from_env()
        settings.validate()
    except Exception:
        logger.exception("campus QA configuration failed; all messages will be ignored")
        return

    tune_oom_score(settings.oom_score_adj)
    application = CampusQaApplication(
        answers=None,
        settings=settings,
    )
    if not await _connect_runtime(settings):
        retry_task = asyncio.create_task(_retry_runtime(settings))


@driver.on_shutdown
async def _shutdown() -> None:
    global application, retry_task, runtime
    application = None
    if retry_task is not None:
        retry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retry_task
        retry_task = None
    if runtime is not None:
        await runtime.close()
        runtime = None


@matcher.handle()
async def _answer(bot: Bot, event: MessageEvent) -> None:
    if application is None:
        return
    if str(event.user_id) == str(bot.self_id):
        return
    # Private messages reach the policy, which decides whether this sender is
    # allowed to use them.  Dropping them here meant LUNA_ALLOWED_USER_IDS could
    # never take effect: the adapter discarded the event before any of it ran.
    in_group = isinstance(event, GroupMessageEvent)
    response = await application.handle(
        InboundMessage(
            message_id=str(event.message_id),
            message_type="group" if in_group else "private",
            group_id=int(event.group_id) if in_group else None,
            user_id=int(event.user_id),
            text=event.get_plaintext(),
        )
    )
    if not response:
        return
    if in_group:
        # Quote and @ the asker, so an answer is readable in a busy group.
        reply = (
            MessageSegment.reply(event.message_id)
            + MessageSegment.at(event.user_id)
            + MessageSegment.text(f" {response}")
        )
    else:
        reply = MessageSegment.text(response)
    await matcher.finish(reply)
