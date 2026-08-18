from grain.engine.guard import GuardConfig


def test_defaults_are_conservative():
    cfg = GuardConfig()
    assert cfg.statement_timeout_ms == 10_000
    assert cfg.row_cap == 10_000
