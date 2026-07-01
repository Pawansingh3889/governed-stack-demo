"""Deterministic compliance validators: the multi-step check runs in Python,
not in the model.

The composite check resolves a batch, pulls its temperature evidence and the
allergen declarations for its product and line, validates both, and returns a
structured verdict. The model never sees raw rows and cannot soften a failure:
it can only narrate a verdict this module already decided and audited.

Governance by construction:
  - no arbitrary SQL -- only the fixed, parameterised queries below ever run;
  - policy (limits, cross-check) is versioned Python, unit-testable;
  - fail-closed -- unknown batch or missing evidence is NON-COMPLIANT;
  - every verdict is appended to a hash-chained, tamper-evident audit log.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

COLD_CHAIN_LIMIT_C = 5.0
POLICY_VERSION = "cc-1.0"


@dataclass
class Finding:
    check: str
    passed: bool
    detail: str
    evidence: list = field(default_factory=list)


@dataclass
class Verdict:
    batch_id: str
    product_code: str
    compliant: bool
    findings: list
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


Q_BATCH = "SELECT batch_id, product_code, line_id FROM batches WHERE batch_id = ?"
Q_TEMPS = "SELECT recorded_at, temp_c FROM temperature_logs WHERE batch_id = ? ORDER BY recorded_at"
Q_DECLARED = "SELECT allergen FROM product_allergens WHERE product_code = ?"
Q_LINE = "SELECT allergen FROM line_allergens WHERE line_id = ?"
Q_LIST = "SELECT batch_id, product_code, line_id FROM batches ORDER BY batch_id"


def check_temperature(readings: list[dict]) -> Finding:
    if not readings:
        return Finding("cold_chain", False, "no temperature evidence on record (fail-closed)")
    breaches = [r for r in readings if r["temp_c"] > COLD_CHAIN_LIMIT_C]
    if breaches:
        return Finding(
            "cold_chain",
            False,
            f"{len(breaches)} reading(s) above the {COLD_CHAIN_LIMIT_C}C limit",
            [f"{b['recorded_at']}: {b['temp_c']}C" for b in breaches],
        )
    return Finding("cold_chain", True, f"all {len(readings)} readings within limit")


def check_allergens(declared: set[str], line_present: set[str]) -> Finding:
    undeclared = sorted(line_present - declared)
    if undeclared:
        return Finding(
            "allergen_crosscheck",
            False,
            "allergen(s) present on the line but not declared on the product",
            undeclared,
        )
    return Finding("allergen_crosscheck", True, "no undeclared allergen exposure")


class ComplianceStore:
    def __init__(self, db_path: str, audit_path: str | None = None):
        self.db_path = db_path
        self.audit_path = audit_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def list_batches(self) -> list[dict]:
        return self._query(Q_LIST)

    def check_batch(self, batch_id: str) -> Verdict:
        rows = self._query(Q_BATCH, (batch_id,))
        if not rows:
            v = Verdict(batch_id, "?", False, [Finding("resolve", False, "unknown batch (fail-closed)")])
            self._audit(v)
            return v

        b = rows[0]
        temps = self._query(Q_TEMPS, (batch_id,))
        declared = {r["allergen"] for r in self._query(Q_DECLARED, (b["product_code"],))}
        line = {r["allergen"] for r in self._query(Q_LINE, (b["line_id"],))}

        findings = [check_temperature(temps), check_allergens(declared, line)]
        v = Verdict(batch_id, b["product_code"], all(f.passed for f in findings), findings)
        self._audit(v)
        return v

    def _audit(self, v: Verdict) -> None:
        if not self.audit_path:
            return
        try:
            prev = ""
            path = Path(self.audit_path)
            if path.exists():
                lines = path.read_text(encoding="utf-8").splitlines()
                if lines:
                    prev = json.loads(lines[-1])["hash"]
            entry = {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "batch_id": v.batch_id,
                "compliant": v.compliant,
                "policy": v.policy_version,
                "prev": prev,
            }
            entry["hash"] = hashlib.sha256((prev + json.dumps(entry, sort_keys=True)).encode()).hexdigest()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # auditing must never crash the check; a real build would alert


def seed_demo(db_path: str) -> None:
    """Create and seed the demo database if it does not exist yet (idempotent)."""
    path = Path(db_path)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE batches(batch_id TEXT PRIMARY KEY, product_code TEXT, line_id TEXT);
        CREATE TABLE temperature_logs(batch_id TEXT, recorded_at TEXT, temp_c REAL);
        CREATE TABLE product_allergens(product_code TEXT, allergen TEXT);
        CREATE TABLE line_allergens(line_id TEXT, allergen TEXT);

        INSERT INTO batches VALUES
          ('B-1001','PROD-A','LINE-1'),
          ('B-1002','PROD-B','LINE-1'),
          ('B-1003','PROD-C','LINE-2');

        INSERT INTO temperature_logs VALUES
          ('B-1001','2026-07-01T06:00',3.1),('B-1001','2026-07-01T09:00',4.4),
          ('B-1002','2026-07-01T06:00',3.6),('B-1002','2026-07-01T09:00',7.2),
          ('B-1003','2026-07-01T06:00',2.9),('B-1003','2026-07-01T09:00',4.0);

        INSERT INTO product_allergens VALUES
          ('PROD-A','Milk'),('PROD-B','Milk'),('PROD-C','Gluten');

        INSERT INTO line_allergens VALUES
          ('LINE-1','Milk'),
          ('LINE-2','Gluten'),('LINE-2','Peanuts');
        """
    )
    conn.commit()
    conn.close()
