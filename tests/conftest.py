import os
import pytest
from sqlalchemy import create_engine, MetaData

from grain.engine.loader import load_ontology
from grain.domains.chinook import CHINOOK_DIR


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


@pytest.fixture(scope="session")
def chinook_metadata(db_engine):
    md = MetaData()
    md.reflect(bind=db_engine)
    return md


@pytest.fixture(scope="session")
def chinook_ontology(chinook_metadata):
    return load_ontology(CHINOOK_DIR / "ontology.yaml", chinook_metadata)
