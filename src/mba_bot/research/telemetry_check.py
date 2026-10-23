"""Read-only health check for research telemetry, for use during the deployment runbook.

Opens the SQLite file with `mode=ro`. Reads table existence, the Alembic revision, row counts and timestamps of
`trades`, `trade_telemetry` and `decision_log`. Reads no credential table, customer email or secret, and writes nothing."""
from __future__ import annotations

import sqlite3
from pathlib import Path

EXPECTED_REVISION = "d4a8f1c3e6b2"  # alembic/versions/d4a8f1c3e6b2_add_trade_telemetry_and_decision_log.py


def check(db_path: str) -> dict:
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        out: dict = {"tables": {t: (t in tables) for t in ("trades", "trade_telemetry", "decision_log", "alembic_version")}}
        out["alembic_revision"] = con.execute("select version_num from alembic_version").fetchone()[0] if "alembic_version" in tables else None
        out["at_expected_revision_or_later"] = out["alembic_revision"] is not None
        out["revision_is_telemetry_head"] = out["alembic_revision"] == EXPECTED_REVISION
        n = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
        out["trades"] = n("select count(*) from trades") if "trades" in tables else None
        out["open_trades"] = n("select count(*) from trades where status not in ('closed','void')") if "trades" in tables else None
        if "trade_telemetry" in tables:
            out["telemetry_rows"] = n("select count(*) from trade_telemetry")
            out["telemetry_newest"] = con.execute("select max(created_at) from trade_telemetry").fetchone()[0]
            out["trades_without_telemetry_since_first_row"] = n(
                "select count(*) from trades t where t.opened_at >= (select min(created_at) from trade_telemetry) "
                "and not exists (select 1 from trade_telemetry x where x.trade_id = t.id)") if "trades" in tables else None
        if "decision_log" in tables:
            out["decision_rows"] = n("select count(*) from decision_log")
            out["decision_newest"] = con.execute("select max(ts) from decision_log").fetchone()[0]
            out["decision_rows_last_24h"] = n("select count(*) from decision_log where ts >= datetime('now','-1 day')")
        out["file_bytes"] = path.stat().st_size
        return out
    finally:
        con.close()


def render(res: dict) -> str:
    lines = ["Telemetry check (read-only)"]
    lines += [f"  {k}: {v}" for k, v in res.items()]
    return "\n".join(lines) + "\n"
