from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BuildError


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    """One immutable byte sequence used for both parsing and attestation."""

    name: str
    payload: bytes
    sha256: str

    @classmethod
    def from_path(cls, path: Path) -> "ArtifactSnapshot":
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise BuildError(f"cannot snapshot {path}: {exc}") from exc
        return cls.from_bytes(str(path.resolve()), payload)

    @classmethod
    def from_bytes(cls, name: str, payload: bytes) -> "ArtifactSnapshot":
        immutable = bytes(payload)
        return cls(name, immutable, hashlib.sha256(immutable).hexdigest())

    def text(self) -> str:
        try:
            return self.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BuildError(f"{self.name}: input is not valid UTF-8") from exc

    def json(self) -> Any:
        try:
            return json.loads(self.text())
        except json.JSONDecodeError as exc:
            raise BuildError(f"{self.name}: invalid JSON: {exc.msg}") from exc

    def write_to(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)

    def assert_path_unchanged(self, path: Path) -> None:
        current = ArtifactSnapshot.from_path(path)
        if current.sha256 != self.sha256:
            raise BuildError(f"input changed while processing: {path}")
