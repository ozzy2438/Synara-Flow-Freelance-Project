from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS applied_events (
    event_id VARCHAR PRIMARY KEY,
    event_type VARCHAR,
    applied_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS fact_orders (
    event_id VARCHAR PRIMARY KEY,
    order_id VARCHAR,
    sku VARCHAR,
    quantity INTEGER,
    unit_price DOUBLE,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS fact_purchase_orders (
    event_id VARCHAR PRIMARY KEY,
    po_id VARCHAR,
    sku VARCHAR,
    quantity INTEGER,
    placed_at TIMESTAMP,
    expected_arrival TIMESTAMP,
    status VARCHAR
);
CREATE TABLE IF NOT EXISTS operational_alerts (
    event_id VARCHAR PRIMARY KEY,
    sku VARCHAR,
    event_type VARCHAR,
    payload JSON,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS dim_products (
    sku VARCHAR PRIMARY KEY,
    name VARCHAR,
    unit_price DOUBLE,
    margin_rate DOUBLE,
    lead_time_days INTEGER,
    category VARCHAR,
    is_high_runner BOOLEAN
);
CREATE TABLE IF NOT EXISTS snapshot_inventory (
    sku VARCHAR PRIMARY KEY,
    on_hand INTEGER,
    on_order INTEGER,
    safety_stock INTEGER,
    reorder_point INTEGER,
    updated_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS simulation_results (
    run_id VARCHAR,
    sku VARCHAR,
    daily_velocity_48h DOUBLE,
    daily_velocity_7d DOUBLE,
    shortfall_units DOUBLE,
    at_risk_gross_usd DOUBLE,
    at_risk_margin_usd DOUBLE,
    recoverable_margin_usd DOUBLE,
    unrecoverable_margin_usd DOUBLE,
    hours_to_stockout DOUBLE,
    lead_time_days INTEGER,
    recommended_po_qty INTEGER,
    po_arrives_after_horizon BOOLEAN,
    demand_multiplier DOUBLE,
    run_at TIMESTAMP
);
"""


class DuckWarehouse:
    """File-backed OLAP sink. The outbox consumer's external system.

    FastAPI and Streamlit do not share an in-memory database: Streamlit talks
    to FastAPI, and only the API/worker process opens this file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connect().close()

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(str(self.path))
        con.execute(SCHEMA_SQL)
        return con

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()
        wal = Path(str(self.path) + ".wal")
        if wal.exists():
            wal.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute(SCHEMA_SQL)

    def already_applied(self, event_id: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM applied_events WHERE event_id = ?", [event_id]
            ).fetchone()
            return row is not None

    def apply_event(self, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if self.already_applied(event_id):
            return
        with self._connect() as con:
            con.execute("BEGIN")
            try:
                if event_type == "order_placed":
                    con.execute(
                        """
                        INSERT INTO fact_orders
                        (event_id, order_id, sku, quantity, unit_price, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            event_id,
                            payload["order_id"],
                            payload["sku"],
                            int(payload["quantity"]),
                            float(payload["unit_price"]),
                            payload["created_at"],
                        ],
                    )
                elif event_type == "purchase_order_placed":
                    con.execute(
                        """
                        INSERT INTO fact_purchase_orders
                        (event_id, po_id, sku, quantity, placed_at, expected_arrival, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            event_id,
                            payload["po_id"],
                            payload["sku"],
                            int(payload["quantity"]),
                            payload["placed_at"],
                            payload["expected_arrival"],
                            payload.get("status", "placed"),
                        ],
                    )
                elif event_type == "inventory_below_rop":
                    con.execute(
                        """
                        INSERT INTO operational_alerts
                        (event_id, sku, event_type, payload, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            event_id,
                            payload["sku"],
                            event_type,
                            json.dumps(payload),
                            payload.get("created_at") or datetime.now(timezone.utc).isoformat(),
                        ],
                    )
                con.execute(
                    "INSERT INTO applied_events (event_id, event_type, applied_at) VALUES (?, ?, ?)",
                    [event_id, event_type, datetime.now(timezone.utc)],
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

    def replace_dimensions(self, products: list[dict], inventory: list[dict]) -> None:
        with self._connect() as con:
            con.execute("BEGIN")
            con.execute("DELETE FROM dim_products")
            con.execute("DELETE FROM snapshot_inventory")
            for p in products:
                con.execute(
                    """
                    INSERT INTO dim_products
                    (sku, name, unit_price, margin_rate, lead_time_days, category, is_high_runner)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        p["sku"],
                        p["name"],
                        p["unit_price"],
                        p["margin_rate"],
                        p["lead_time_days"],
                        p["category"],
                        p["is_high_runner"],
                    ],
                )
            for inv in inventory:
                con.execute(
                    """
                    INSERT INTO snapshot_inventory
                    (sku, on_hand, on_order, safety_stock, reorder_point, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        inv["sku"],
                        inv["on_hand"],
                        inv["on_order"],
                        inv["safety_stock"],
                        inv["reorder_point"],
                        inv["updated_at"],
                    ],
                )
            con.execute("COMMIT")

    def replace_facts_from_lists(
        self, orders: list[dict], purchase_orders: list[dict]
    ) -> None:
        """Used by seed so DuckDB matches Postgres without replaying thousands of outbox rows."""
        with self._connect() as con:
            con.execute("BEGIN")
            con.execute("DELETE FROM fact_orders")
            con.execute("DELETE FROM fact_purchase_orders")
            for o in orders:
                con.execute(
                    """
                    INSERT INTO fact_orders
                    (event_id, order_id, sku, quantity, unit_price, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        o["event_id"],
                        o["order_id"],
                        o["sku"],
                        o["quantity"],
                        o["unit_price"],
                        o["created_at"],
                    ],
                )
            for po in purchase_orders:
                con.execute(
                    """
                    INSERT INTO fact_purchase_orders
                    (event_id, po_id, sku, quantity, placed_at, expected_arrival, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        po["event_id"],
                        po["po_id"],
                        po["sku"],
                        po["quantity"],
                        po["placed_at"],
                        po["expected_arrival"],
                        po["status"],
                    ],
                )
            con.execute("COMMIT")

    def velocity_rows(self, now: datetime) -> list[dict[str, Any]]:
        from datetime import timedelta

        t48 = now - timedelta(hours=48)
        t7 = now - timedelta(days=7)
        with self._connect() as con:
            result = con.execute(
                """
                SELECT
                    p.sku,
                    p.name,
                    p.unit_price,
                    p.margin_rate,
                    p.lead_time_days,
                    p.category,
                    p.is_high_runner,
                    i.on_hand,
                    i.on_order,
                    i.safety_stock,
                    i.reorder_point,
                    COALESCE(SUM(CASE WHEN o.created_at >= ? THEN o.quantity ELSE 0 END) / 2.0, 0) AS daily_velocity_48h,
                    COALESCE(SUM(CASE WHEN o.created_at >= ? THEN o.quantity ELSE 0 END) / 7.0, 0) AS daily_velocity_7d
                FROM dim_products p
                JOIN snapshot_inventory i ON i.sku = p.sku
                LEFT JOIN fact_orders o ON o.sku = p.sku AND o.created_at >= ?
                GROUP BY 1,2,3,4,5,6,7,8,9,10,11
                """,
                [t48, t7, t7],
            ).fetchall()
            cols = [
                "sku",
                "name",
                "unit_price",
                "margin_rate",
                "lead_time_days",
                "category",
                "is_high_runner",
                "on_hand",
                "on_order",
                "safety_stock",
                "reorder_point",
                "daily_velocity_48h",
                "daily_velocity_7d",
            ]
            return [dict(zip(cols, row)) for row in result]

    def holdout_mape(self, now: datetime) -> dict[str, float | int | None]:
        """Score the 48h walk against the last 48h of actual sales.

        Train on (now-96h, now-48h] vs naive 7d ending at now-48h.
        Actual is (now-48h, now]. SKUs with no actual sales are skipped.
        """
        from datetime import timedelta

        t48 = now - timedelta(hours=48)
        t96 = now - timedelta(hours=96)
        t7 = t48 - timedelta(days=7)
        with self._connect() as con:
            row = con.execute(
                """
                WITH agg AS (
                    SELECT
                        sku,
                        SUM(CASE WHEN created_at >= ? AND created_at < ? THEN quantity ELSE 0 END) / 2.0
                            AS v48,
                        SUM(CASE WHEN created_at >= ? AND created_at < ? THEN quantity ELSE 0 END) / 7.0
                            AS v7,
                        SUM(CASE WHEN created_at >= ? THEN quantity ELSE 0 END) AS actual
                    FROM fact_orders
                    WHERE created_at >= ?
                    GROUP BY sku
                )
                SELECT
                    AVG(CASE WHEN actual > 0 THEN abs(v48 * 2.0 - actual) / actual END),
                    AVG(CASE WHEN actual > 0 THEN abs(v7 * 2.0 - actual) / actual END),
                    COUNT(*) FILTER (WHERE actual > 0)
                FROM agg
                """,
                [t96, t48, t7, t48, t48, t7],
            ).fetchone()
        mape_48, mape_7, n = row if row else (None, None, 0)
        n = int(n or 0)
        return {
            "mape_48h_velocity": round(float(mape_48), 3) if mape_48 is not None else None,
            "mape_7d_naive": round(float(mape_7), 3) if mape_7 is not None else None,
            "n_skus": n,
            "note": (
                "Holdout: last 48h actual vs a walk trained on the prior 48h. "
                "Not a fitted model. Prior stockouts censor demand, so MAPE is a floor."
            ),
        }

    def inbound_by_sku_hour(
        self, now: datetime, horizon_hours: int
    ) -> dict[str, dict[int, float]]:
        from datetime import timedelta

        horizon_end = now + timedelta(hours=horizon_hours)
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT sku, expected_arrival, quantity
                FROM fact_purchase_orders
                WHERE status = 'placed'
                  AND expected_arrival > ?
                  AND expected_arrival <= ?
                """,
                [now, horizon_end],
            ).fetchall()
        inbound: dict[str, dict[int, float]] = {}
        for sku, arrival, qty in rows:
            if hasattr(arrival, "tzinfo") and arrival.tzinfo is None:
                arrival = arrival.replace(tzinfo=timezone.utc)
            delta_h = int((arrival - now).total_seconds() // 3600)
            hour = max(1, min(horizon_hours, delta_h if delta_h > 0 else 1))
            inbound.setdefault(sku, {})
            inbound[sku][hour] = inbound[sku].get(hour, 0.0) + float(qty)
        return inbound

    def write_simulation(self, run_id: str, run_at: datetime, rows: list[dict]) -> None:
        with self._connect() as con:
            con.execute("BEGIN")
            con.execute("DELETE FROM simulation_results")
            for r in rows:
                con.execute(
                    """
                    INSERT INTO simulation_results VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        run_id,
                        r["sku"],
                        r["daily_velocity_48h"],
                        r["daily_velocity_7d"],
                        r["shortfall_units"],
                        r["at_risk_gross_usd"],
                        r["at_risk_margin_usd"],
                        r["recoverable_margin_usd"],
                        r["unrecoverable_margin_usd"],
                        r.get("hours_to_stockout"),
                        r["lead_time_days"],
                        r["recommended_po_qty"],
                        r["po_arrives_after_horizon"],
                        r["demand_multiplier"],
                        run_at,
                    ],
                )
            con.execute("COMMIT")

    def operational_alerts(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT event_id, sku, event_type, payload, created_at
                FROM operational_alerts
                ORDER BY created_at DESC
                LIMIT 100
                """
            ).fetchall()
            return [
                {
                    "event_id": r[0],
                    "sku": r[1],
                    "event_type": r[2],
                    "payload": json.loads(r[3]) if isinstance(r[3], str) else r[3],
                    "created_at": r[4].isoformat() if hasattr(r[4], "isoformat") else str(r[4]),
                }
                for r in rows
            ]
