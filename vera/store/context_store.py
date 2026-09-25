"""Versioned, thread-safe context store.

Semantics (per challenge-testing-brief §2.1 and api-call-examples 1.4-1.6):
  * first version for (scope, context_id)      -> stored, accepted
  * higher version                             -> replaces atomically, accepted
  * same version                               -> no-op, 409 stale_version
  * lower version                              -> no-op, 409 stale_version (newer data kept)
Payload dicts are treated as immutable once stored; readers get the same object.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Optional

from ..utils.timeutil import iso_now

SCOPES = ("category", "merchant", "customer", "trigger")


@dataclass(frozen=True)
class ContextRecord:
    scope: str
    context_id: str
    version: int
    payload: dict
    delivered_at: str
    stored_at: str
    digest: str


@dataclass(frozen=True)
class UpsertResult:
    accepted: bool
    record: Optional[ContextRecord]
    current_version: Optional[int]
    reason: str = ""


def payload_digest(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


class ContextStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, ContextRecord]] = {s: {} for s in SCOPES}
        self._generation = 0  # bumps on every accepted write; used for cache keys

    def upsert(self, scope: str, context_id: str, version: int, payload: dict, delivered_at: str = "") -> UpsertResult:
        if scope not in self._data:
            raise ValueError(f"unsupported scope: {scope}")
        record = ContextRecord(
            scope=scope,
            context_id=context_id,
            version=version,
            payload=payload,
            delivered_at=delivered_at,
            stored_at=iso_now(),
            digest=payload_digest(payload),
        )
        with self._lock:
            current = self._data[scope].get(context_id)
            if current is not None and current.version >= version:
                return UpsertResult(False, current, current.version, "stale_version")
            self._data[scope][context_id] = record  # single reference swap = atomic replace
            self._generation += 1
            return UpsertResult(True, record, version)

    def get(self, scope: str, context_id: Optional[str]) -> Optional[dict]:
        rec = self.get_record(scope, context_id)
        return rec.payload if rec else None

    def get_record(self, scope: str, context_id: Optional[str]) -> Optional[ContextRecord]:
        if not context_id or scope not in self._data:
            return None
        with self._lock:
            return self._data[scope].get(str(context_id))

    def version(self, scope: str, context_id: Optional[str]) -> int:
        rec = self.get_record(scope, context_id)
        return rec.version if rec else 0

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {s: len(self._data[s]) for s in SCOPES}

    def ids(self, scope: str) -> list[str]:
        with self._lock:
            return sorted(self._data.get(scope, {}).keys())

    def records(self, scope: str) -> list[ContextRecord]:
        with self._lock:
            return list(self._data.get(scope, {}).values())

    @property
    def generation(self) -> int:
        return self._generation

    def clear(self) -> None:
        with self._lock:
            self._data = {s: {} for s in SCOPES}
            self._generation += 1
