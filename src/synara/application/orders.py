from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from synara.domain import InsufficientStockError, SkuNotFoundError
from synara.infrastructure.orm import OrderRow, OutboxEventRow, ProductRow, lock_inventory


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _outbox(
    session: Session,
    *,
    event_type: str,
    aggregate_id: str,
    payload: dict,
    created_at: datetime,
    max_attempts: int = 8,
) -> OutboxEventRow:
    row = OutboxEventRow(
        id=uuid.uuid4(),
        event_type=event_type,
        aggregate_id=aggregate_id,
        payload=payload,
        status="pending",
        attempts=0,
        max_attempts=max_attempts,
        available_at=created_at,
        created_at=created_at,
    )
    session.add(row)
    return row


def place_order(
    session: Session,
    *,
    sku: str,
    quantity: int,
    idempotency_key: str,
    created_at: datetime | None = None,
) -> OrderRow:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    created_at = created_at or _now()

    existing = session.query(OrderRow).filter_by(idempotency_key=idempotency_key).one_or_none()
    if existing is not None:
        return existing

    product = session.get(ProductRow, sku)
    if product is None:
        raise SkuNotFoundError(sku)

    inventory = lock_inventory(session, sku)
    if inventory is None:
        raise SkuNotFoundError(sku)

    if inventory.on_hand < quantity:
        raise InsufficientStockError(sku, quantity, inventory.on_hand)

    was_above = inventory.on_hand > inventory.reorder_point
    inventory.on_hand -= quantity
    inventory.updated_at = created_at
    crossed_rop = was_above and inventory.on_hand <= inventory.reorder_point

    order = OrderRow(
        id=uuid.uuid4(),
        sku=sku,
        quantity=quantity,
        unit_price=product.unit_price,
        created_at=created_at,
        idempotency_key=idempotency_key,
    )
    session.add(order)

    _outbox(
        session,
        event_type="order_placed",
        aggregate_id=sku,
        payload={
            "order_id": str(order.id),
            "sku": sku,
            "quantity": quantity,
            "unit_price": product.unit_price,
            "on_hand_after": inventory.on_hand,
            "created_at": created_at.isoformat(),
        },
        created_at=created_at,
    )
    if crossed_rop:
        _outbox(
            session,
            event_type="inventory_below_rop",
            aggregate_id=sku,
            payload={
                "sku": sku,
                "on_hand": inventory.on_hand,
                "reorder_point": inventory.reorder_point,
                "order_id": str(order.id),
                "created_at": created_at.isoformat(),
            },
            created_at=created_at,
        )
    return order
