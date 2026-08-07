from pathlib import Path

from luna_kb.pipeline.cli import (
    _embedding_key,
    load_embedding_cache,
    save_embedding_cache,
)


class _Card:
    def __init__(self, text: str, embedding: list[float] | None = None) -> None:
        self._text = text
        self.embedding = embedding

    def search_text(self) -> str:
        return self._text


class _Item:
    def __init__(self, card: _Card) -> None:
        self.card = card


def test_vectors_survive_a_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "cache.bin"
    items = [_Item(_Card("奖学金怎么申请", [0.5] * 4)), _Item(_Card("无向量的卡"))]

    assert save_embedding_cache(path, items, 4) == 1

    cached = load_embedding_cache(path, 4)
    assert list(cached) == [_embedding_key(items[0].card)]


def test_editing_a_card_misses_the_cache(tmp_path: Path) -> None:
    # The key is the text that gets embedded, so a card whose wording changed is
    # re-embedded rather than served a vector for what it used to say.
    path = tmp_path / "cache.bin"
    save_embedding_cache(path, [_Item(_Card("原来的说法", [0.5] * 4))], 4)

    cached = load_embedding_cache(path, 4)

    assert _embedding_key(_Card("改过的说法")) not in cached


def test_a_damaged_cache_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    # The cache is an optimisation.  The right answer to a broken one is to
    # embed again, not to fail a build.
    path = tmp_path / "cache.bin"
    save_embedding_cache(path, [_Item(_Card("一张卡", [0.5] * 4))], 4)

    assert load_embedding_cache(path, 8) == {}

    path.write_bytes(b"not a cache at all")
    assert load_embedding_cache(path, 4) == {}
    assert load_embedding_cache(tmp_path / "missing.bin", 4) == {}
