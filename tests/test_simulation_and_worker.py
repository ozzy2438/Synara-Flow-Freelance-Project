from synara.application.orders import place_order
from synara.application.replenishment import place_emergency_po
from synara.application.seed import seed_world
from synara.application.simulate import run_simulation
from synara.infrastructure.db import session_scope
from synara.infrastructure.worker import process_batch
from tests.conftest import insert_sku


def test_spike_skus_flagged_with_48h_velocity_not_washed_out(factory, warehouse, settings):
    with session_scope(factory) as session:
        info = seed_world(session, warehouse, seed=42)
    with session_scope(factory) as session:
        result = run_simulation(session, warehouse)
    spiked = set(info["spiked_skus"])
    at_risk = {row["sku"] for row in result["at_risk"]}
    assert spiked & at_risk, f"expected spike SKUs in at-risk, got {at_risk}"
    for row in result["at_risk"]:
        if row["sku"] in spiked:
            assert row["daily_velocity_48h"] > row["daily_velocity_7d"]
            assert row["at_risk_margin_usd"] > 0


def test_worker_applies_outbox_idempotently_to_duckdb(factory, warehouse, settings):
    with session_scope(factory) as session:
        insert_sku(session, on_hand=15, rop=20, price=40.0, margin=0.3)
    with session_scope(factory) as session:
        place_order(session, sku="SKU-001", quantity=1, idempotency_key="w1")
    n = process_batch(factory, warehouse, settings)
    assert n >= 1
    assert warehouse.already_applied is not None
    n2 = process_batch(factory, warehouse, settings)
    assert n2 == 0
    with warehouse._connect() as con:
        count = con.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0]
    assert count == 1


def test_emergency_po_changes_next_simulation(factory, warehouse, settings):
    with session_scope(factory) as session:
        insert_sku(session, on_hand=0, rop=10, price=50.0, margin=0.4, lead=7, safety=2)
        # Give DuckDB an order history so 48h velocity is non-zero.
        from datetime import datetime, timedelta, timezone
        from synara.infrastructure.orm import OrderRow
        import uuid

        now = datetime.now(timezone.utc)
        for i in range(10):
            session.add(
                OrderRow(
                    id=uuid.uuid4(),
                    sku="SKU-001",
                    quantity=5,
                    unit_price=50.0,
                    created_at=now - timedelta(hours=i * 4),
                    idempotency_key=f"hist-{i}",
                )
            )
        warehouse.replace_dimensions(
            [
                {
                    "sku": "SKU-001",
                    "name": "Test Item",
                    "unit_price": 50.0,
                    "margin_rate": 0.4,
                    "lead_time_days": 7,
                    "category": "test",
                    "is_high_runner": True,
                }
            ],
            [
                {
                    "sku": "SKU-001",
                    "on_hand": 0,
                    "on_order": 0,
                    "safety_stock": 2,
                    "reorder_point": 10,
                    "updated_at": now,
                }
            ],
        )
        warehouse.replace_facts_from_lists(
            [
                {
                    "event_id": f"h-{i}",
                    "order_id": f"h-{i}",
                    "sku": "SKU-001",
                    "quantity": 5,
                    "unit_price": 50.0,
                    "created_at": now - timedelta(hours=i * 4),
                }
                for i in range(10)
            ],
            [],
        )
    with session_scope(factory) as session:
        before = run_simulation(session, warehouse)
    assert before["totals"]["at_risk_margin_usd"] > 0
    with session_scope(factory) as session:
        place_emergency_po(session, sku="SKU-001", quantity=80)
    process_batch(factory, warehouse, settings)
    with session_scope(factory) as session:
        after = run_simulation(session, warehouse)
    sku_after = next(r for r in after["all_skus"] if r["sku"] == "SKU-001")
    sku_before = next(r for r in before["all_skus"] if r["sku"] == "SKU-001")
    assert sku_after["on_order"] >= 80
    assert sku_after["shortfall_units"] < sku_before["shortfall_units"]
