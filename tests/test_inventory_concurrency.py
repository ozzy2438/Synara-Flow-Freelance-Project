from concurrent.futures import ThreadPoolExecutor, as_completed

from synara.application.orders import place_order
from synara.domain import InsufficientStockError
from synara.infrastructure.db import session_scope
from synara.infrastructure.orm import InventoryRow
from tests.conftest import insert_sku


def test_concurrent_orders_do_not_oversell(factory):
    with session_scope(factory) as session:
        insert_sku(session, on_hand=10, rop=1)

    def attempt(key: str) -> str:
        try:
            with session_scope(factory) as session:
                place_order(session, sku="SKU-001", quantity=8, idempotency_key=key)
            return "ok"
        except InsufficientStockError:
            return "reject"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, f"c-{i}") for i in range(2)]
        results = [f.result() for f in as_completed(futures)]

    assert results.count("ok") == 1
    assert results.count("reject") == 1
    with session_scope(factory) as session:
        assert session.get(InventoryRow, "SKU-001").on_hand == 2
