"""Allergen name canonicalization via the Open Food Facts allergen taxonomy.

The naive failure mode of an allergen cross-check is vocabulary: the line
record says "Lactose", the product spec says "Milk", and a raw string compare
either misses a real exposure or false-alarms on a declared one. The Open Food
Facts taxonomy (https://world.openfoodfacts.org, data under the Open Database
License) maps synonyms in 30+ languages onto canonical allergen ids -- lactose,
whey and butter are all en:milk; barley is en:gluten -- so both sides of the
check are compared in canonical terms.

A vendored snapshot of the taxonomy ships with the stack (fetched from
https://static.openfoodfacts.org/data/taxonomies/allergens.full.json), so the
check stays offline-first. Unknown terms are NOT dropped: they canonicalize to
a raw: fallback id, so an allergen the taxonomy has never heard of still
counts in the comparison -- fail closed, never fail silent.
"""
from __future__ import annotations

import json
from pathlib import Path


class AllergenTaxonomy:
    def __init__(self, path: str | None):
        self._syn_to_id: dict[str, str] = {}
        self._display: dict[str, str] = {}
        if path and Path(path).exists():
            self._load(path)

    def _load(self, path: str) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for canon_id, entry in data.items():
            names = entry.get("name") or {}
            self._display[canon_id] = names.get("en") or canon_id
            for lang_names in names.values():
                self._syn_to_id.setdefault(str(lang_names).strip().lower(), canon_id)
            for lang_syns in (entry.get("synonyms") or {}).values():
                for syn in lang_syns:
                    self._syn_to_id.setdefault(str(syn).strip().lower(), canon_id)

    @property
    def loaded(self) -> bool:
        return bool(self._syn_to_id)

    def canonicalize(self, term: str) -> str:
        """Map an allergen name to its canonical taxonomy id.

        Unknown terms return a raw: id built from the lowercased input, so
        they still participate in comparisons instead of vanishing.
        """
        key = term.strip().lower()
        return self._syn_to_id.get(key, f"raw:{key}")

    def display(self, canon_id: str) -> str:
        """Human-readable name for a canonical id (raw: ids show their term)."""
        if canon_id.startswith("raw:"):
            return canon_id[4:]
        return self._display.get(canon_id, canon_id)
