from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import Select, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
from sqlalchemy.types import DateTime, Float, Integer, String, Text, Boolean

import uuid


class Base(DeclarativeBase):
    pass


class ProductRow(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    margin_rate: Mapped[float] = mapped_column(Float, nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    is_high_runner: Mapped[bool] = mapped_column(Boolean, default=False)
    substitute_sku: Mapped[str | None] = mapped_column(String(32), nullable=True)
    substitute_capture: Mapped[float | None] = mapped_column(Float, nullable=True)


class InventoryRow(Base):
    __tablename__ = "inventory"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False)
    on_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safety_stock: Mapped[int] = mapped_column(Integer, nullable=False)
    reorder_point: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sku: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)


class PurchaseOrderRow(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sku: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="placed")
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_arrival: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    buyer_email: Mapped[str | None] = mapped_column(String(128), nullable=True)


class OpsState(Base):
    """Single-row operating contract: where the world came from."""

    __tablename__ = "ops_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="synthetic")
    source_label: Mapped[str] = mapped_column(String(256), nullable=False)


class OutboxEventRow(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def lock_inventory(session: Session, sku: str) -> InventoryRow | None:
    stmt: Select[tuple[InventoryRow]] = (
        select(InventoryRow).where(InventoryRow.sku == sku).with_for_update()
    )
    return session.execute(stmt).scalar_one_or_none()


def claim_outbox_batch(
    session: Session, *, batch_size: int, worker_id: str, now: datetime
) -> Sequence[OutboxEventRow]:
    """Claim pending/failed rows with SKIP LOCKED. Caller owns the transaction."""
    ids_stmt = (
        select(OutboxEventRow.id)
        .where(
            OutboxEventRow.status.in_(("pending", "failed")),
            OutboxEventRow.available_at <= now,
        )
        .order_by(OutboxEventRow.created_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    ids = list(session.execute(ids_stmt).scalars().all())
    if not ids:
        return []
    session.execute(
        update(OutboxEventRow)
        .where(OutboxEventRow.id.in_(ids))
        .values(locked_by=worker_id, locked_at=now)
    )
    rows = session.execute(
        select(OutboxEventRow).where(OutboxEventRow.id.in_(ids)).order_by(OutboxEventRow.created_at)
    ).scalars().all()
    return rows


def wipe_oltp(session: Session) -> None:
    session.execute(delete(OutboxEventRow))
    session.execute(delete(OrderRow))
    session.execute(delete(PurchaseOrderRow))
    session.execute(delete(InventoryRow))
    session.execute(delete(ProductRow))
    session.execute(delete(OpsState))


def set_ops_state(session: Session, *, mode: str, source_label: str) -> OpsState:
    row = session.get(OpsState, 1)
    if row is None:
        row = OpsState(id=1, mode=mode, source_label=source_label)
        session.add(row)
    else:
        row.mode = mode
        row.source_label = source_label
    return row


def get_ops_state(session: Session) -> OpsState | None:
    return session.get(OpsState, 1)


def count_pending_outbox(session: Session) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(OutboxEventRow).where(
                OutboxEventRow.status.in_(("pending", "failed"))
            )
        ).scalar_one()
    )


def pg_now(session: Session) -> datetime:
    return session.execute(text("SELECT NOW()")).scalar_one()
