from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("synara.simulate")

from sqlalchemy.orm import Session

from synara.domain.revenue import split_revenue
from synara.infrastructure.duckdb_warehouse import DuckWarehouse
from synara.infrastructure.orm import InventoryRow, ProductRow, PurchaseOrderRow


def refresh_olap_snapshot(session: Session, warehouse: DuckWarehouse) -> None:
    products = session.query(ProductRow).all()
    inventory = session.query(InventoryRow).all()
    warehouse.replace_dimensions(
        [
            {
                "sku": p.sku,
                "name": p.name,
                "unit_price": p.unit_price,
                "margin_rate": p.margin_rate,
                "lead_time_days": p.lead_time_days,
                "category": p.category,
                "is_high_runner": p.is_high_runner,
            }
            for p in products
        ],
        [
            {
                "sku": i.sku,
                "on_hand": i.on_hand,
                "on_order": i.on_order,
                "safety_stock": i.safety_stock,
                "reorder_point": i.reorder_point,
                "updated_at": i.updated_at,
            }
            for i in inventory
        ],
    )
    pos = session.query(PurchaseOrderRow).filter(PurchaseOrderRow.status == "placed").all()
    with warehouse._connect() as con:
        con.execute("DELETE FROM fact_purchase_orders")
        for po in pos:
            con.execute(
                """
                INSERT INTO fact_purchase_orders
                (event_id, po_id, sku, quantity, placed_at, expected_arrival, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    f"oltp-po-{po.id}",
                    str(po.id),
                    po.sku,
                    po.quantity,
                    po.placed_at,
                    po.expected_arrival,
                    po.status,
                ],
            )


def run_simulation(
    session: Session,
    warehouse: DuckWarehouse,
    *,
    horizon_hours: int = 48,
    demand_multiplier: float = 1.0,
    lead_time_delta_days: int = 0,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    refresh_olap_snapshot(session, warehouse)
    velocity = warehouse.velocity_rows(now)
    inbound = warehouse.inbound_by_sku_hour(now, horizon_hours)
    results = []
    for row in velocity:
        lead = max(0, int(row["lead_time_days"]) + lead_time_delta_days)
        split = split_revenue(
            sku=row["sku"],
            unit_price=float(row["unit_price"]),
            margin_rate=float(row["margin_rate"]),
            on_hand=int(row["on_hand"]),
            on_order=int(row["on_order"]),
            daily_velocity=float(row["daily_velocity_48h"]),
            horizon_hours=horizon_hours,
            lead_time_days=lead,
            safety_stock=int(row["safety_stock"]),
            inbound_by_hour=inbound.get(row["sku"], {}),
            demand_multiplier=demand_multiplier,
            lead_time_override_days=lead,
        )
        results.append(
            {
                "sku": row["sku"],
                "name": row["name"],
                "category": row["category"],
                "is_high_runner": bool(row["is_high_runner"]),
                "on_hand": int(row["on_hand"]),
                "on_order": int(row["on_order"]),
                "reorder_point": int(row["reorder_point"]),
                "unit_price": float(row["unit_price"]),
                "margin_rate": float(row["margin_rate"]),
                "daily_velocity_48h": float(row["daily_velocity_48h"]),
                "daily_velocity_7d": float(row["daily_velocity_7d"]),
                "shortfall_units": split.shortfall_units,
                "at_risk_gross_usd": round(split.at_risk_gross_usd, 2),
                "at_risk_margin_usd": round(split.at_risk_margin_usd, 2),
                "recoverable_margin_usd": round(split.recoverable_margin_usd, 2),
                "unrecoverable_margin_usd": round(split.unrecoverable_margin_usd, 2),
                "hours_to_stockout": split.hours_to_stockout,
                "lead_time_days": split.lead_time_days,
                "recommended_po_qty": split.recommended_po_qty,
                "po_arrives_after_horizon": split.po_arrives_after_horizon,
                "demand_multiplier": demand_multiplier,
            }
        )

    at_risk = [r for r in results if r["shortfall_units"] > 0]
    at_risk.sort(key=lambda r: r["at_risk_margin_usd"], reverse=True)
    run_id = str(uuid.uuid4())
    warehouse.write_simulation(run_id, now, results)
    totals = {
        "at_risk_sku_count": len(at_risk),
        "at_risk_gross_usd": round(sum(r["at_risk_gross_usd"] for r in at_risk), 2),
        "at_risk_margin_usd": round(sum(r["at_risk_margin_usd"] for r in at_risk), 2),
        "recoverable_margin_usd": round(sum(r["recoverable_margin_usd"] for r in at_risk), 2),
        "unrecoverable_margin_usd": round(
            sum(r["unrecoverable_margin_usd"] for r in at_risk), 2
        ),
    }
    logger.info(
        "simulation_complete at_risk_skus=%s at_risk_margin_usd=%s "
        "recoverable_margin_usd=%s unrecoverable_margin_usd=%s demand_mult=%s",
        totals["at_risk_sku_count"],
        totals["at_risk_margin_usd"],
        totals["recoverable_margin_usd"],
        totals["unrecoverable_margin_usd"],
        demand_multiplier,
    )
    return {
        "run_id": run_id,
        "as_of": now.isoformat(),
        "horizon_hours": horizon_hours,
        "demand_multiplier": demand_multiplier,
        "lead_time_delta_days": lead_time_delta_days,
        "totals": totals,
        "at_risk": at_risk,
        "all_skus": results,
        "operational_alerts": warehouse.operational_alerts(),
        "metric_note": (
            "recoverable_margin_usd is the portion a 24h expedite PO placed now "
            "can still cover inside the horizon. unrecoverable_margin_usd is demand "
            "that hits before that freight can arrive. Standard supplier lead time "
            "is still shown per SKU; if it exceeds 48h, po_arrives_after_horizon "
            "is true (a non-expedite PO cannot save this window). "
            "This is not list-price 'saved revenue'."
        ),
    }
