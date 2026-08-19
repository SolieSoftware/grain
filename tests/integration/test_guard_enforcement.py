"""Proofs that the guard actually does what it claims against a real
database -- not just that its code path runs without raising."""
import pytest
from sqlalchemy import create_engine, literal_column, select, text
from sqlalchemy.exc import OperationalError

from grain.engine.errors import GuardTripped
from grain.engine.execute import execute
from grain.engine.guard import GuardConfig, guarded_connection

pytestmark = pytest.mark.integration


def test_row_cap_trips_when_a_statement_has_no_limit(db_engine):
    """This is the guard's actual job: catching a statement that reaches the
    database with no LIMIT at all -- e.g. a compiler bug that dropped the
    spec's own LIMIT clause, or (now that QuerySpec.limit has no upper bound)
    a caller who legitimately asked for `limit=None`. `generate_series` has
    no LIMIT of its own and returns every row it's asked for unless the
    guard stops it."""
    stmt = select(literal_column("generate_series(1, 50)").label("n"))
    with pytest.raises(GuardTripped) as exc:
        execute(db_engine, stmt, GuardConfig(row_cap=5))
    assert exc.value.limit_name == "row_cap"
    assert exc.value.limit_value == 5


def test_statement_timeout_is_actually_enforced_by_postgres(db_engine):
    """Not just that `guarded_connection` runs `SET LOCAL` without error --
    that Postgres itself cancels a statement which overruns it. `pg_sleep(2)`
    against a 200ms timeout must be cancelled well before it would finish."""
    stmt = select(text("pg_sleep(2)"))
    with pytest.raises(OperationalError) as exc:
        execute(db_engine, stmt, GuardConfig(statement_timeout_ms=200))
    assert "statement timeout" in str(exc.value).lower()


def test_timeout_applies_even_on_an_autocommit_engine(db_url):
    """The mode the bug lived in. Under AUTOCOMMIT every statement is its own
    transaction, so SET LOCAL evaporates before the next one -- and conn.begin()
    alone does not fix it. The guard pins the isolation level for exactly this.
    Without that pin this test hangs for the full sleep instead of cancelling."""
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
    try:
        with pytest.raises(OperationalError, match="statement timeout"):
            with guarded_connection(engine, GuardConfig(statement_timeout_ms=200)) as conn:
                conn.execute(text("select pg_sleep(2)"))
    finally:
        engine.dispose()
