from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from synara.domain import SkuNotFoundError
from synara.domain.revenue import recommended_po_quantity
from synara.infrastructure.orm import ProductRow, PurchaseOrderRow, lock_inventory
from synara.application.orders import _outbox


def place_emergency_po(
    session: Session,
    *,
    sku: str,
    quantity: int | None = None,
    placed_at: datetime | None = None,
    buyer_email: str = "purchasing@company.example",
) -> PurchaseOrderRow:
    placed_at = placed_at or datetime.now(timezone.utc)
    product = session.get(ProductRow, sku)
    if product is None:
        raise SkuNotFoundError(sku)
    inventory = lock_inventory(session, sku)
    if inventory is None:
        raise SkuNotFoundError(sku)

    qty = quantity
    if qty is None:
        qty = recommended_po_quantity(
            daily_velocity=max(inventory.reorder_point / max(product.lead_time_days, 1), 1.0),
            lead_time_days=product.lead_time_days,
            safety_stock=inventory.safety_stock,
            on_hand=inventory.on_hand,
            on_order=inventory.on_order,
        )
        if qty <= 0:
            qty = max(inventory.safety_stock, 1)
    if qty <= 0:
        raise ValueError("quantity must be positive")

    # Expedite freight (24h), not standard supplier lead time — so the 48h walk
    # can show recovered units. Standard lead_time_days still drives the
    # unrecoverable split when no PO is placed.
    arrival = placed_at + timedelta(hours=24)
    po = PurchaseOrderRow(
        id=uuid.uuid4(),
        sku=sku,
        quantity=qty,
        status="placed",
        placed_at=placed_at,
        expected_arrival=arrival,
        buyer_email=buyer_email,
    )
    session.add(po)
    inventory.on_order += qty
    inventory.updated_at = placed_at
    _outbox(
        session,
        event_type="purchase_order_placed",
        aggregate_id=sku,
        payload={
            "po_id": str(po.id),
            "sku": sku,
            "quantity": qty,
            "placed_at": placed_at.isoformat(),
            "expected_arrival": arrival.isoformat(),
            "status": "placed",
            "lead_time_days": product.lead_time_days,
            "expedite_hours": 24,
        },
        created_at=placed_at,
    )
    return po


def po_number(po: PurchaseOrderRow) -> str:
    return f"SYN-{po.id.hex[:8].upper()}"


def export_po(session: Session, po_id: str) -> dict:
    try:
        uid = uuid.UUID(po_id)
    except ValueError as exc:
        raise ValueError("invalid po_id") from exc
    po = session.get(PurchaseOrderRow, uid)
    if po is None:
        raise LookupError(po_id)
    product = session.get(ProductRow, po.sku)
    po.exported_at = datetime.now(timezone.utc)
    number = po_number(po)
    to = po.buyer_email or "purchasing@company.example"
    csv_body = (
        "po_number,sku,name,quantity,expected_arrival,channel\n"
        f"{number},{po.sku},{product.name if product else ''},{po.quantity},"
        f"{po.expected_arrival.isoformat()},csv_email_not_erp\n"
    )
    email = (
        f"To: {to}\n"
        f"Subject: Expedite PO {number} — {po.sku} x {po.quantity}\n\n"
        f"Please expedite {po.quantity} units of {po.sku}"
        f"{' (' + product.name + ')' if product else ''} for arrival "
        f"{po.expected_arrival.isoformat()}.\n\n"
        "This is a purchasing draft from Synara. It has not been posted to an ERP.\n"
    )
    return {
        "po_id": str(po.id),
        "po_number": number,
        "sku": po.sku,
        "quantity": po.quantity,
        "buyer_email": to,
        "expected_arrival": po.expected_arrival.isoformat(),
        "exported_at": po.exported_at.isoformat(),
        "channel": "csv_email_not_erp",
        "csv": csv_body,
        "email": email,
    }
