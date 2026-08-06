from pathlib import Path

import pytest

from luna_kb.attestation import ArtifactSnapshot
from luna_kb.errors import BuildError


def test_artifact_snapshot_hashes_the_same_bytes_that_are_parsed(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text('{"version":1}\n', encoding="utf-8")

    snapshot = ArtifactSnapshot.from_path(path)
    path.write_text('{"version":2}\n', encoding="utf-8")

    assert snapshot.json() == {"version": 1}
    with pytest.raises(BuildError, match="input changed while processing"):
        snapshot.assert_path_unchanged(path)


def test_artifact_snapshot_rejects_invalid_utf8() -> None:
    snapshot = ArtifactSnapshot.from_bytes("broken.json", b"\xff")

    with pytest.raises(BuildError, match="valid UTF-8"):
        snapshot.text()

