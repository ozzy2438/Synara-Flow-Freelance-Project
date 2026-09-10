from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from synara.application.orders import place_order
from synara.domain import InsufficientStockError
from synara.infrastructure.db import session_scope
from synara.infrastructure.orm import InventoryRow, OrderRow, OutboxEventRow
from tests.conftest import insert_sku


def test_outbox_commits_with_order(factory):
    with session_scope(factory) as session:
        insert_sku(session, on_hand=20, rop=5)
    with session_scope(factory) as session:
        place_order(session, sku="SKU-001", quantity=3, idempotency_key="k1")
    with session_scope(factory) as session:
        orders = session.execute(select(OrderRow)).scalars().all()
        events = session.execute(select(OutboxEventRow)).scalars().all()
        inv = session.get(InventoryRow, "SKU-001")
        assert len(orders) == 1
        assert orders[0].quantity == 3
        assert inv.on_hand == 17
        types = {e.event_type for e in events}
        assert "order_placed" in types
        assert all(e.status == "pending" for e in events)


def test_outbox_rolls_back_when_transaction_fails(factory):
    with session_scope(factory) as session:
        insert_sku(session, on_hand=20, rop=5)
    with pytest.raises(RuntimeError, match="boom"):
        with session_scope(factory) as session:
            place_order(session, sku="SKU-001", quantity=3, idempotency_key="k-fail")
            raise RuntimeError("boom")
    with session_scope(factory) as session:
        assert session.execute(select(OrderRow)).scalars().all() == []
        assert session.execute(select(OutboxEventRow)).scalars().all() == []
        assert session.get(InventoryRow, "SKU-001").on_hand == 20


def test_crossing_rop_emits_second_event(factory):
    with session_scope(factory) as session:
        insert_sku(session, on_hand=10, rop=8)
    with session_scope(factory) as session:
        place_order(session, sku="SKU-001", quantity=3, idempotency_key="k-rop")
    with session_scope(factory) as session:
        types = [
            e.event_type
            for e in session.execute(select(OutboxEventRow)).scalars().all()
        ]
        assert types.count("order_placed") == 1
        assert types.count("inventory_below_rop") == 1


def test_oversell_rejected_and_no_outbox(factory):
    with session_scope(factory) as session:
        insert_sku(session, on_hand=2, rop=1)
    with pytest.raises(InsufficientStockError):
        with session_scope(factory) as session:
            place_order(session, sku="SKU-001", quantity=5, idempotency_key="k-os")
    with session_scope(factory) as session:
        assert session.execute(select(OrderRow)).scalars().all() == []
        assert session.execute(select(OutboxEventRow)).scalars().all() == []
        assert session.get(InventoryRow, "SKU-001").on_hand == 2


def test_idempotent_order_key(factory):
    with session_scope(factory) as session:
        insert_sku(session, on_hand=20)
    with session_scope(factory) as session:
        a = place_order(session, sku="SKU-001", quantity=2, idempotency_key="same")
    with session_scope(factory) as session:
        b = place_order(session, sku="SKU-001", quantity=2, idempotency_key="same")
        assert str(a.id) == str(b.id)
        assert session.get(InventoryRow, "SKU-001").on_hand == 18
