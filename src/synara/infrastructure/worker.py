from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from synara.config import Settings, get_settings
from synara.infrastructure.db import create_schema, make_engine, make_session_factory
from synara.infrastructure.duckdb_warehouse import DuckWarehouse
from synara.infrastructure.orm import claim_outbox_batch

logger = logging.getLogger("synara.worker")


def process_batch(session_factory: sessionmaker, warehouse: DuckWarehouse, settings: Settings) -> int:
    now = datetime.now(timezone.utc)
    processed = 0
    with session_factory() as session:
        with session.begin():
            rows = claim_outbox_batch(
                session,
                batch_size=settings.outbox_batch_size,
                worker_id=settings.worker_id,
                now=now,
            )
            for row in rows:
                event_id = str(row.id)
                try:
                    warehouse.apply_event(event_id, row.event_type, row.payload)
                    row.status = "processed"
                    row.processed_at = now
                    row.attempts += 1
                    row.last_error = None
                    processed += 1
                    logger.info(
                        "outbox_applied",
                        extra={
                            "event_id": event_id,
                            "event_type": row.event_type,
                            "aggregate_id": row.aggregate_id,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 — recorded on the row
                    row.attempts += 1
                    row.last_error = str(exc)[:2000]
                    backoff = timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
                    row.available_at = now + backoff
                    row.status = (
                        "dead_letter" if row.attempts >= row.max_attempts else "failed"
                    )
                    logger.exception(
                        "outbox_apply_failed event_id=%s attempts=%s",
                        event_id,
                        row.attempts,
                    )
    return processed


def run_forever(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)
    engine = make_engine(settings.database_url)
    create_schema(engine)
    factory = make_session_factory(engine)
    warehouse = DuckWarehouse(settings.duckdb_file)
    logger.info("outbox worker started id=%s duckdb=%s", settings.worker_id, settings.duckdb_file)
    while True:
        n = process_batch(factory, warehouse, settings)
        if n == 0:
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    run_forever()
