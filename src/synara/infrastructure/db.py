from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from synara.infrastructure.orm import Base


def make_engine(database_url: str, *, echo: bool = False) -> Engine:
    return create_engine(database_url, echo=echo, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def is_retryable_db_error(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate in {"40001", "40P01"}:
        return True
    msg = str(exc).lower()
    return "deadlock" in msg or "could not serialize" in msg or "lock timeout" in msg


def retry_transaction(fn: Callable[[], None], *, attempts: int = 4, pause_s: float = 0.05) -> None:
    import time

    last: BaseException | None = None
    for i in range(attempts):
        try:
            fn()
            return
        except Exception as exc:  # noqa: BLE001 — inspect then re-raise
            last = exc
            if not is_retryable_db_error(exc) or i == attempts - 1:
                raise
            time.sleep(pause_s * (2**i))
    if last:
        raise last
