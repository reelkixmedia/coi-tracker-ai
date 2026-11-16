"""Database plumbing, adapted from the Recovery Engine's app/db.py (same naive-UTC rule, same create_all +
add-missing-columns approach, same Postgres URL normalising for Render/Neon)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def utcnow() -> datetime:
    """Naive UTC timestamp. Everything in the database is stored as naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_db_url(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = normalize_db_url(get_settings().database_url)
        kwargs = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs["pool_pre_ping"] = True
            kwargs["pool_recycle"] = 300
        _engine = create_engine(url, **kwargs)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def SessionLocal():
    get_engine()
    return _SessionLocal()


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    from . import models  # noqa: F401  (registers tables)

    engine = get_engine()
    Base.metadata.create_all(engine)
    add_missing_columns(engine)


def add_missing_columns(engine) -> list[str]:
    """Adds columns the models define that the live database lacks. Only ever ADDS; never drops or alters."""
    from sqlalchemy import inspect, literal, text

    added = []
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"
                default = getattr(col.default, "arg", None)
                if default is not None and not callable(default):
                    lit = literal(default, type_=col.type).compile(dialect=engine.dialect,
                                                                    compile_kwargs={"literal_binds": True})
                    ddl += f" DEFAULT {lit}"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    return added
