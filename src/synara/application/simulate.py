from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synara.application.ingest import last_sale_at
from synara.domain.revenue import split_revenue
from synara.infrastructure.duckdb_warehouse import DuckWarehouse
from synara.infrastructure.orm import (
    InventoryRow,
    ProductRow,
    PurchaseOrderRow,
    get_ops_state,
)

logger = logging.getLogger("synara.simulate")

SITE = "Main DC — one warehouse. Transfers are not modeled."
PO_CHANNEL = "CSV + email draft. Not posted to an ERP."
SCALE = (
    "Sized for 50–5,000 SKUs on this API box (DuckDB file). "
    "Not 50k SKUs / 10 sites — that would move OLAP off the request path."
)


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


def _apply_substitution(results: list[dict], products: list[ProductRow], lost_sale_rate: float) -> None:
    by_sku = {r["sku"]: r for r in results}
    subs = {p.sku: (p.substitute_sku, p.substitute_capture or 0.0) for p in products}
    for r in results:
        alt, cap = subs.get(r["sku"], (None, 0.0))
        r["substitute_sku"] = alt
        r["substitute_capture"] = cap or None
        r["margin_after_substitution_usd"] = r["at_risk_margin_usd"]
        if not alt or not cap or r["shortfall_units"] <= 0:
            continue
        other = by_sku.get(alt)
        if other is None:
            continue
        leftover = other["on_hand"] - other["daily_velocity_48h"] * 2.0 * other["demand_multiplier"]
        captured = min(r["shortfall_units"] * cap, max(0.0, leftover))
        r["margin_after_substitution_usd"] = round(
            max(
                0.0,
                r["at_risk_margin_usd"]
                - captured * r["unit_price"] * r["margin_rate"] * lost_sale_rate,
            ),
            2,
        )


def run_simulation(
    session: Session,
    warehouse: DuckWarehouse,
    *,
    horizon_hours: int = 48,
    demand_multiplier: float = 1.0,
    lead_time_delta_days: int = 0,
    lost_sale_rate: float = 1.0,
    now: datetime | None = None,
) -> dict:
    t0 = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    lost_sale_rate = min(1.0, max(0.0, lost_sale_rate))
    refresh_olap_snapshot(session, warehouse)
    velocity = warehouse.velocity_rows(now)
    inbound = warehouse.inbound_by_sku_hour(now, horizon_hours)
    products = session.query(ProductRow).all()
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
        ls = lost_sale_rate
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
                "at_risk_gross_usd": round(split.at_risk_gross_usd * ls, 2),
                "at_risk_margin_usd": round(split.at_risk_margin_usd * ls, 2),
                "recoverable_margin_usd": round(split.recoverable_margin_usd * ls, 2),
                "unrecoverable_margin_usd": round(split.unrecoverable_margin_usd * ls, 2),
                "hours_to_stockout": split.hours_to_stockout,
                "lead_time_days": split.lead_time_days,
                "recommended_po_qty": split.recommended_po_qty,
                "po_arrives_after_horizon": split.po_arrives_after_horizon,
                "demand_multiplier": demand_multiplier,
            }
        )

    _apply_substitution(results, products, lost_sale_rate)
    at_risk = [r for r in results if r["shortfall_units"] > 0]
    at_risk.sort(key=lambda r: r["at_risk_margin_usd"], reverse=True)
    run_id = str(uuid.uuid4())
    warehouse.write_simulation(run_id, now, results)
    totals = {
        "at_risk_sku_count": len(at_risk),
        "at_risk_gross_usd": round(sum(r["at_risk_gross_usd"] for r in at_risk), 2),
        "at_risk_margin_usd": round(sum(r["at_risk_margin_usd"] for r in at_risk), 2),
        "recoverable_margin_usd": round(sum(r["recoverable_margin_usd"] for r in at_risk), 2),
        "unrecoverable_margin_usd": round(sum(r["unrecoverable_margin_usd"] for r in at_risk), 2),
        "at_risk_margin_after_substitution_usd": round(
            sum(r["margin_after_substitution_usd"] for r in at_risk), 2
        ),
    }
    ops = get_ops_state(session)
    sale_at = last_sale_at(session)
    sku_count = session.execute(select(func.count()).select_from(ProductRow)).scalar_one()
    holdout = warehouse.holdout_mape(now)
    compute_ms = int((time.perf_counter() - t0) * 1000)
    contract = {
        "mode": ops.mode if ops else "unknown",
        "source": ops.source_label if ops else "No catalog loaded",
        "as_of": now.isoformat(),
        "last_sale_at": sale_at.isoformat() if sale_at else None,
        "sku_count": int(sku_count),
        "site": SITE,
        "lost_sale_rate": lost_sale_rate,
        "po_channel": PO_CHANNEL,
        "scale": SCALE,
        "holdout": holdout,
        "compute_ms": compute_ms,
    }
    logger.info(
        "simulation_complete at_risk_skus=%s at_risk_margin_usd=%s "
        "recoverable_margin_usd=%s mape_48h=%s compute_ms=%s",
        totals["at_risk_sku_count"],
        totals["at_risk_margin_usd"],
        totals["recoverable_margin_usd"],
        holdout.get("mape_48h_velocity"),
        compute_ms,
    )
    return {
        "run_id": run_id,
        "as_of": now.isoformat(),
        "horizon_hours": horizon_hours,
        "demand_multiplier": demand_multiplier,
        "lead_time_delta_days": lead_time_delta_days,
        "lost_sale_rate": lost_sale_rate,
        "operating_contract": contract,
        "totals": totals,
        "at_risk": at_risk,
        "all_skus": results,
        "operational_alerts": warehouse.operational_alerts(),
        "metric_note": (
            "At-risk figures are contribution margin × lost-sale rate "
            f"({lost_sale_rate:.0%} assumed lost, rest treated as delay). "
            "Recoverable assumes 24h expedite freight, not standard supplier lead time. "
            "Substitution, if set on the catalog, only reduces margin_after_substitution_usd."
        ),
    }
