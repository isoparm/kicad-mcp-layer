"""Stable identifiers for generated designs.

KiCad gives every symbol, wire, footprint, pad and track a UUID. A generator that draws fresh
random ones on each run rewrites every line of every file it emits, so a rebuild of unchanged
input is not byte-identical, diffs are unreadable and a refactor cannot be shown to be harmless.

:class:`IdFactory` derives identifiers from what an item *is*: a name-based UUID (version 5) of
a scope plus the item's own key (reference, pin number, coordinates, net...). Two runs with the
same input produce the same identifiers. Items whose keys collide, such as two identical wires,
are numbered in order of creation, which is itself deterministic for a generator.

Without a scope the factory hands out random UUIDs, which is right for editing files that
already hold identifiers we did not create.
"""
from __future__ import annotations

import uuid

NAMESPACE = uuid.UUID("2c6b1c5a-5e0d-4a6f-9a8b-4f1e7d3c2b10")


class IdFactory:
    def __init__(self, scope: str | None = None) -> None:
        self.scope = scope
        self._counts: dict[str, int] = {}

    def make(self, *key: object) -> str:
        """The identifier for the item described by ``key``; the n-th identical key gets the n-th id."""
        if self.scope is None:
            return str(uuid.uuid4())
        k = "/".join(_atom(p) for p in key)
        n = self._counts.get(k, 0)
        self._counts[k] = n + 1
        return str(uuid.uuid5(NAMESPACE, f"{self.scope}|{k}#{n}"))

    def child(self, *key: object) -> "IdFactory":
        """A factory for a nested document (a sub-sheet), scoped under this one."""
        if self.scope is None:
            return IdFactory()
        return IdFactory(self.scope + "/" + "/".join(_atom(p) for p in key))


def _atom(p: object) -> str:
    if isinstance(p, float):
        return repr(round(p, 6))
    return str(p)
