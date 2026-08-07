"""Observed-graph node keys -> DataHub URNs.

Deliberately explicit. v1 does **no** fuzzy matching, no string similarity, no
"close enough" heuristics: a node either has a declared mapping or it is
reported as unmapped. Polygraph's whole claim is that it tells you the truth
about lineage, so it must not invent correspondences that a human never stated.

Two forms of mapping, both explicit:

* ``nodes:``    exact key -> URN
* ``patterns:`` a shell glob -> URN, for keys that legitimately vary between
  runs (``file:runs/healthy/predictions.csv`` vs ``file:runs/buggy/...``).
  Globs are matched in file order and the first hit wins.

Anything unmatched lands in ``unmapped`` and is surfaced in the report rather
than silently dropped -- an unmapped observed node is itself a finding.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class UrnMapError(ValueError):
    pass


@dataclass
class UrnMap:
    exact: dict[str, str] = field(default_factory=dict)
    patterns: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "UrnMap":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        exact = dict(raw.get("nodes") or {})

        patterns: list[tuple[str, str]] = []
        for i, entry in enumerate(raw.get("patterns") or []):
            if not isinstance(entry, dict) or "match" not in entry or "urn" not in entry:
                raise UrnMapError(
                    f"patterns[{i}] must be a mapping with 'match' and 'urn' keys, got {entry!r}"
                )
            patterns.append((str(entry["match"]), str(entry["urn"])))

        for key, urn in exact.items():
            if not str(urn).startswith("urn:li:"):
                raise UrnMapError(f"nodes[{key!r}] is not a DataHub URN: {urn!r}")
        for match, urn in patterns:
            if not urn.startswith("urn:li:"):
                raise UrnMapError(f"patterns match={match!r} is not a DataHub URN: {urn!r}")

        return cls(exact=exact, patterns=patterns)

    def resolve(self, key: str) -> str | None:
        if key in self.exact:
            return self.exact[key]
        for match, urn in self.patterns:
            if fnmatch.fnmatchcase(key, match):
                return urn
        return None

    def map_graph(self, graph: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
        """Return ``(key -> urn, unmapped_keys)`` for every node in the graph."""
        mapped: dict[str, str] = {}
        unmapped: list[str] = []
        for node in graph.get("nodes", []):
            key = node["key"]
            urn = self.resolve(key)
            if urn:
                mapped[key] = urn
            else:
                unmapped.append(key)
        return mapped, sorted(unmapped)
