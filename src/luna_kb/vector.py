from __future__ import annotations

import sqlite3
import math
from array import array
from collections.abc import Sequence

from .errors import BuildError, RetrievalUnavailable


def load_sqlite_vec(connection: sqlite3.Connection, *, build: bool = False) -> None:
    try:
        import sqlite_vec

        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        connection.execute("SELECT vec_version()").fetchone()
    except Exception as exc:  # extension errors differ across platforms
        error = BuildError if build else RetrievalUnavailable
        if build:
            raise error(f"sqlite-vec is required: {exc}") from exc
        raise error("sqlite-vec", str(exc)) from exc


def serialize_float32(vector: Sequence[float]) -> bytes:
    floats = [float(value) for value in vector]
    if not floats or any(not math.isfinite(value) for value in floats):
        raise ValueError("vector must contain only finite values")
    if not any(value != 0 for value in floats):
        raise ValueError("vector cannot be all zeros")
    values = array("f", floats)
    return values.tobytes()
