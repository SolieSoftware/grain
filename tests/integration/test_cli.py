import json
import subprocess
import sys
import pytest

pytestmark = pytest.mark.integration


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "grain.engine.cli", *args],
                          capture_output=True, text=True, check=True)


def test_describe_emits_json_with_the_rules():
    out = json.loads(run_cli("describe").stdout)
    assert "non_additivity" in out["rules"]


def test_explain_emits_sql_without_executing():
    spec = json.dumps({"object": "Customer", "group_by": ["country"],
                       "metrics": ["customer_count"]})
    out = json.loads(run_cli("explain", "--spec", spec).stdout)
    assert "SELECT" in out["compiled_sql"].upper()
