"""Persist fixed parts and UUIDs; retries reuse exactly the same bytes and recipient."""

import asyncio
import random
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app import payloads
from app.db import DeliveryPart, Run, SessionLocal, now
from app.leases import assert_owner
from app.metrics import retry
from app.pipeline import PipelineError, bot_token, send_part
from app.runlog import event
from app.security import digest


# 持久化固定分段、摘要与幂等 UUID，再按序发送，已确认送达的分段直接跳过。
# 未知结果最多重试四次且复用原内容和 UUID；首次尝试五十分钟后停止继续发送。
async def deliver(run_id, recipient, parts):
    with SessionLocal() as db:
        run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        assert_owner(run)
        assert run is not None
        existing = list(
            db.scalars(
                select(DeliveryPart)
                .where(DeliveryPart.run_id == run_id)
                .order_by(DeliveryPart.part)
            )
        )
        if existing:
            if len(existing) != len(parts) or any(
                p.payload_hash != digest(parts[p.part - 1]) for p in existing
            ):
                raise PipelineError("PAYLOAD_MISMATCH")
        else:
            # Write payload first: a crash here can recollect safely because no send has been attempted.
            payloads.save(run, parts)
            for index, part in enumerate(parts, 1):
                db.add(
                    DeliveryPart(
                        run_id=run_id,
                        part=index,
                        uuid=str(uuid5(NAMESPACE_URL, f"{run_id}:report:{index}")),
                        payload_hash=digest(part),
                        status="pending",
                    )
                )
            run.delivery_parts_count = len(parts)
        db.commit()
    from app.runner import stage

    stage(run_id, "sending")
    access = await bot_token()
    for index, content in enumerate(parts, 1):
        for attempt in range(4):
            if attempt:
                retry("delivery")
            with SessionLocal() as db:
                run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
                assert_owner(run)
                assert run is not None
                from app.runner import permitted

                if not permitted(db, run):
                    raise PipelineError("CANCELLED")
                part = db.scalar(
                    select(DeliveryPart).where(
                        DeliveryPart.run_id == run_id, DeliveryPart.part == index
                    )
                )
                assert part is not None
                if part.status == "delivered":
                    break
                if part.first_attempt_at and now() >= part.first_attempt_at + timedelta(minutes=50):
                    raise PipelineError("DELIVERY_UNKNOWN", uncertain=True)
                part.first_attempt_at = part.first_attempt_at or now()
                # 外部请求前提交发送意图；进程中断时才能识别“可能送达”，而非当作未发送。
                part.status = "sending"
                run.execution_status, run.delivery_status = "sending", "sending"
                key = part.uuid
                db.commit()
            event("delivery_started", part=index, parts=len(parts), attempt=attempt + 1)
            try:
                message_id = await send_part(
                    access, recipient, content, key, msg_type="interactive"
                )
            except PipelineError as error:
                event("delivery_failed", part=index, code=error.code)
                if not error.uncertain:
                    with SessionLocal() as db:
                        run = db.get(Run, run_id)
                        assert_owner(run)
                        part = db.scalar(select(DeliveryPart).where(DeliveryPart.uuid == key))
                        assert part is not None
                        part.status = "failed"
                        db.commit()
                    raise
                if attempt == 3:
                    raise
                await asyncio.sleep([5, 20, 60][attempt] + random.random())
                continue
            # 收到确认后再次核验租约，再落库送达结果；旧执行者不能覆盖新任务状态。
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                assert_owner(run)
                part = db.scalar(select(DeliveryPart).where(DeliveryPart.uuid == key))
                assert part is not None
                part.status, part.message_id = "delivered", message_id
                db.commit()
            event("delivery_succeeded", part=index, parts=len(parts))
            await asyncio.sleep(1)
            break
