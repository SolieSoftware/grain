import json
import subprocess
import sys
import pytest

pytestmark = pytest.mark.integration


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "grain.engine.cli", *args],
                          capture_output=True, text=True, check=True)


# Both tests take `db_url` purely to SKIP without a database rather than fail.
# They shell out with `check=True`, so without it the subprocess exits 2 ("GRAIN_
# DATABASE_URL is not set") and CalledProcessError surfaces as a FAILURE -- a red
# baseline on a machine that simply has no database, which trains a reader to
# ignore failures. The fixture is requested, not used: the CLI reads the URL from
# the environment itself.
def test_describe_emits_json_with_the_rules(db_url):
    out = json.loads(run_cli("describe").stdout)
    assert "non_additivity" in out["rules"]


def test_explain_emits_sql_without_executing(db_url):
    spec = json.dumps({"object": "Customer", "group_by": ["country"],
                       "metrics": ["customer_count"]})
    out = json.loads(run_cli("explain", "--spec", spec).stdout)
    assert "SELECT" in out["compiled_sql"].upper()
