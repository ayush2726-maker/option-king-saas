from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_signal_route_does_not_run_schema_migrations_per_refresh():
    source = (ROOT / "bot" / "routes.py").read_text(encoding="utf-8")
    signal_route = source.split('@router.get("/signal")', 1)[1].split(
        '@router.get("/debug-state")', 1
    )[0]

    assert "ensure_tables(conn)" not in signal_route
    assert "log_signal_snapshot(" not in signal_route


def test_bot_schema_runs_once_during_application_startup():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    startup = source.split("def startup():", 1)[1].split(
        "@app.get(\"/\")", 1
    )[0]

    assert "ensure_bot_tables(bot_conn)" in startup
    assert "bot_conn.close()" in startup


def test_runtime_persists_signal_history_outside_the_get_route():
    source = (ROOT / "bot" / "routes.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "def log_signal_snapshot" in source
    assert "apply_score_history_patch()" in main_source
