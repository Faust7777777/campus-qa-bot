from __future__ import annotations


ALL_CAMPUSES = frozenset({"凌水", "开发区", "盘锦"})


def campus_values(value: str) -> frozenset[str]:
    value = value.strip()
    if value in {"", "全校"}:
        return ALL_CAMPUSES
    return frozenset(part for part in value.split("|") if part)


def parent_scope_covers_child(
    *,
    parent_validity: str,
    parent_campus: str,
    parent_audience: str,
    child_validity: str,
    child_campus: str,
    child_audience: str,
) -> bool:
    """Whether parent evidence is safe everywhere the child can be selected."""

    return (
        parent_validity == child_validity
        and campus_values(parent_campus).issuperset(campus_values(child_campus))
        and (not parent_audience or parent_audience == child_audience)
    )


def matches_query_scope(
    *,
    validity: str,
    campus: str,
    audience: str,
    time_scope: str,
    requested_campus: str,
    requested_audience: str,
) -> bool:
    historical_match = (
        validity == "historical"
        if time_scope == "historical"
        else validity != "historical"
    )
    campus_match = (
        not requested_campus or requested_campus in campus_values(campus)
    )
    audience_match = not requested_audience or audience in {"", requested_audience}
    return historical_match and campus_match and audience_match
