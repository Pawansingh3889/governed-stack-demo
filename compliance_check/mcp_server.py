"""compliance-check: expose governed compliance verdicts as MCP tools.

The whole multi-step validation (cold chain + allergen cross-check) runs
deterministically in ``core`` before the model sees anything. The tools return
computed verdicts, never raw rows, so the model can only narrate a decision the
code already made -- and every decision lands in a hash-chained audit log.

Configuration (environment):
  COMPLIANCE_DB        SQLite path (seeded with demo data on first run if missing)
  COMPLIANCE_AUDIT     hash-chained audit JSONL path (default: alongside the db)
  COMPLIANCE_TAXONOMY  Open Food Facts allergen taxonomy JSON (default: alongside
                       the db); allergen names on both sides of the cross-check
                       canonicalize through it, so Lactose matches a Milk
                       declaration and Barley is caught by Gluten
"""
from __future__ import annotations

import os
from pathlib import Path

from fastmcp import FastMCP

from compliance_check.allergens import AllergenTaxonomy
from compliance_check.core import ComplianceStore, seed_demo

_DB = os.environ.get("COMPLIANCE_DB", "data/compliance/demo.db")
_AUDIT = os.environ.get("COMPLIANCE_AUDIT", str(Path(_DB).with_name("compliance-audit.jsonl")))
_TAXONOMY = os.environ.get("COMPLIANCE_TAXONOMY", str(Path(_DB).with_name("allergens_full.json")))

seed_demo(_DB)
_store = ComplianceStore(_DB, _AUDIT, taxonomy=AllergenTaxonomy(_TAXONOMY))

mcp = FastMCP("compliance-check")


@mcp.tool()
def batch_compliance(batch_id: str) -> dict:
    """Composite compliance verdict for a batch: cold-chain temperatures plus an
    allergen cross-check (line vs product declaration). The verdict is computed
    deterministically and audited; unknown batches fail closed."""
    return _store.check_batch(batch_id).to_dict()


@mcp.tool()
def list_batches() -> list[dict]:
    """List batches available for a compliance check (id, product, line)."""
    return _store.list_batches()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
