from __future__ import annotations

from dataclasses import dataclass


FORMAL_MINIMUM_KIND_COUNTS = {
    "answerable": 120,
    "historical": 20,
    "no_answer": 40,
    "out_of_scope": 30,
    "faculty_boundary": 30,
}

# The supplied kb_faculty.csv is a fixed, isolated 85-row probe set.  It is
# never production knowledge, but a formal release must prove that the whole
# probe set was audited rather than accepting an empty replacement file.
FORMAL_MINIMUM_FACULTY_ROWS = 85
FORMAL_FACULTY_SET_SHA256 = (
    "de65b2ba498aae2248671a644ee8b741ca4b1406cd8d85bf313336a4dff69b43"
)


@dataclass(frozen=True, slots=True)
class EvaluationThresholds:
    recall_at_50: float = 0.97
    recall_at_5: float = 0.90
    answer_card_match_rate: float = 1.0
    official_source_rate: float = 1.0
    unsupported_conclusions: int = 0
    fabricated_links: int = 0
    faculty_leakage: int = 0
    no_answer_restraint: float = 0.95
    out_of_scope_restraint: float = 0.95
    faculty_boundary_restraint: float = 1.0
    p95_latency_seconds: float = 10.0
