import pytest
from sqlalchemy import func, select

from grain.domains.chinook import models

pytestmark = pytest.mark.integration

EXPECTED_ROWS = {
    "track": 3503, "invoice_line": 2240, "playlist_track": 8715, "invoice": 412,
    "album": 347, "artist": 275, "customer": 59, "genre": 25,
    "playlist": 18, "employee": 8, "media_type": 5,
}


def test_all_tables_present_with_expected_rows(db_engine):
    with db_engine.connect() as conn:
        for table_name, expected in EXPECTED_ROWS.items():
            table = models.Base.metadata.tables[table_name]
            actual = conn.execute(select(func.count()).select_from(table)).scalar_one()
            assert actual == expected, f"{table_name}: expected {expected}, got {actual}"
