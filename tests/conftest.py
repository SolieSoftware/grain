import os
import pytest
from sqlalchemy import create_engine


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("GRAIN_DATABASE_URL")
    if not url:
        pytest.skip("GRAIN_DATABASE_URL not set")
    return url


@pytest.fixture(scope="session")
def db_engine(db_url):
    engine = create_engine(db_url, future=True)
    yield engine
    engine.dispose()
