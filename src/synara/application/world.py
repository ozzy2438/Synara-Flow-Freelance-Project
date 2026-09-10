"""Load / dump the OLTP world. Seed and CSV ingest share this path."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from synara.infrastructure.duckdb_warehouse import DuckWarehouse
from synara.infrastructure.orm import (
    InventoryRow,
    OrderRow,
    ProductRow,
    set_ops_state,
    wipe_oltp,
)


def replace_world(
    session: Session,
    warehouse: DuckWarehouse,
    *,
    products: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    now: datetime,
    mode: str,
    source_label: str,
) -> None:
    wipe_oltp(session)
    warehouse.reset()
    products_payload = []
    inventory_payload = []
    for p in products:
        session.add(
            ProductRow(
                sku=p["sku"],
                name=p["name"],
                unit_price=float(p["unit_price"]),
                margin_rate=float(p["margin_rate"]),
                lead_time_days=int(p["lead_time_days"]),
                category=p.get("category") or "general",
                is_high_runner=bool(p.get("is_high_runner", False)),
                substitute_sku=p.get("substitute_sku") or None,
                substitute_capture=p.get("substitute_capture"),
            )
        )
        session.add(
            InventoryRow(
                sku=p["sku"],
                on_hand=int(p["on_hand"]),
                on_order=int(p.get("on_order") or 0),
                safety_stock=int(p["safety_stock"]),
                reorder_point=int(p["reorder_point"]),
                updated_at=now,
            )
        )
        products_payload.append(
            {
                "sku": p["sku"],
                "name": p["name"],
                "unit_price": p["unit_price"],
                "margin_rate": p["margin_rate"],
                "lead_time_days": p["lead_time_days"],
                "category": p.get("category") or "general",
                "is_high_runner": bool(p.get("is_high_runner", False)),
            }
        )
        inventory_payload.append(
            {
                "sku": p["sku"],
                "on_hand": p["on_hand"],
                "on_order": p.get("on_order") or 0,
                "safety_stock": p["safety_stock"],
                "reorder_point": p["reorder_point"],
                "updated_at": now,
            }
        )

    fact_orders = []
    for i, o in enumerate(orders):
        oid = str(o.get("order_id") or uuid.uuid4())
        try:
            order_uuid = uuid.UUID(oid)
        except ValueError:
            order_uuid = uuid.uuid4()
            oid = str(order_uuid)
        session.add(
            OrderRow(
                id=order_uuid,
                sku=o["sku"],
                quantity=int(o["quantity"]),
                unit_price=float(o["unit_price"]),
                created_at=o["created_at"],
                idempotency_key=str(o.get("idempotency_key") or f"load:{oid}:{i}"),
            )
        )
        fact_orders.append(
            {
                "event_id": f"load-order-{oid}",
                "order_id": oid,
                "sku": o["sku"],
                "quantity": o["quantity"],
                "unit_price": o["unit_price"],
                "created_at": o["created_at"],
            }
        )
    session.flush()
    warehouse.replace_dimensions(products_payload, inventory_payload)
    warehouse.replace_facts_from_lists(fact_orders, [])
    set_ops_state(session, mode=mode, source_label=source_label)
