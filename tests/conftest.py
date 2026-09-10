from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from synara.config import Settings
from synara.infrastructure.db import create_schema, make_engine, make_session_factory, session_scope
from synara.infrastructure.duckdb_warehouse import DuckWarehouse
from synara.infrastructure.orm import Base, InventoryRow, ProductRow


TEST_DB = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://synara:synara@localhost:5432/synara_test",
)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = make_engine(TEST_DB)
    Base.metadata.drop_all(eng)
    create_schema(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    Base.metadata.drop_all(engine)
    create_schema(engine)
    return make_session_factory(engine)


@pytest.fixture
def warehouse(tmp_path: Path) -> DuckWarehouse:
    return DuckWarehouse(tmp_path / "synara.duckdb")


@pytest.fixture
def settings(engine: Engine, tmp_path: Path) -> Settings:
    return Settings(
        database_url=TEST_DB,
        duckdb_path=str(tmp_path / "synara.duckdb"),
        api_key="test-key",
    )


def insert_sku(
    session: Session,
    sku: str = "SKU-001",
    *,
    on_hand: int = 20,
    price: float = 10.0,
    margin: float = 0.4,
    lead: int = 7,
    rop: int = 8,
    safety: int = 3,
) -> None:
    session.add(
        ProductRow(
            sku=sku,
            name="Test Item",
            unit_price=price,
            margin_rate=margin,
            lead_time_days=lead,
            category="test",
            is_high_runner=True,
        )
    )
    session.add(
        InventoryRow(
            sku=sku,
            on_hand=on_hand,
            on_order=0,
            safety_stock=safety,
            reorder_point=rop,
        )
    )
