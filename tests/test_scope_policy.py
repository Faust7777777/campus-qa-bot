import pytest

from luna_kb.scope_policy import parent_scope_covers_child


@pytest.mark.parametrize(
    ("parent_validity", "parent_campus", "parent_audience"),
    [
        ("historical", "凌水", "本科生"),
        ("current", "盘锦", "本科生"),
        ("current", "凌水", "研究生"),
    ],
)
def test_parent_evidence_must_cover_child_time_campus_and_audience(
    parent_validity: str,
    parent_campus: str,
    parent_audience: str,
) -> None:
    assert not parent_scope_covers_child(
        parent_validity=parent_validity,
        parent_campus=parent_campus,
        parent_audience=parent_audience,
        child_validity="current",
        child_campus="凌水",
        child_audience="本科生",
    )


def test_school_wide_parent_evidence_can_cover_a_campus_specific_child() -> None:
    assert parent_scope_covers_child(
        parent_validity="current",
        parent_campus="全校",
        parent_audience="",
        child_validity="current",
        child_campus="凌水",
        child_audience="本科生",
    )
