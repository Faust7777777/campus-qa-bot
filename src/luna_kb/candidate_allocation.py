from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


class RerankCandidate(Protocol):
    card_id: str
    source_id: str
    card_kind: str
    subject_key: str
    fact_key: str


@dataclass(frozen=True, slots=True)
class RerankCandidateAllocator:
    """Allocate a bounded, diverse reranker set from an ordered RRF pool."""

    budget: int = 16
    max_sources_per_fact: int = 2
    reserve_alternate_slots: int = 1
    # Navigation cards are the overwhelming majority of the reachable knowledge
    # base, so a single reserved slot handed the reranker one arbitrary entry
    # point - whichever the first stage happened to rank highest - and no way to
    # choose between entry points at all.  Several are kept instead, dropping
    # repeats from one source so a source that published the same notice twice
    # cannot spend the whole quota.
    navigation_slots: int = 3

    def __post_init__(self) -> None:
        if self.budget <= 0:
            raise ValueError("reranker budget must be positive")
        if self.max_sources_per_fact <= 0:
            raise ValueError("max_sources_per_fact must be positive")
        if self.reserve_alternate_slots < 0:
            raise ValueError("reserved alternate slots cannot be negative")
        if self.navigation_slots < 0:
            raise ValueError("navigation slots cannot be negative")

    @staticmethod
    def _fact_identity(card: RerankCandidate) -> tuple[str, str, str]:
        if card.card_kind == "fact" and card.subject_key and card.fact_key:
            return ("fact", card.subject_key, card.fact_key)
        return ("card", card.card_id, "")

    def allocate(
        self,
        ordered_ids: Sequence[str],
        cards: Mapping[str, RerankCandidate],
    ) -> list[str]:
        navigation_ids: list[str] = []
        navigation_sources: set[str] = set()
        primary: list[tuple[str, tuple[str, str, str], str]] = []
        alternates: dict[
            tuple[str, str, str], list[tuple[str, tuple[str, str, str], str]]
        ] = {}
        primary_identities: set[tuple[str, str, str]] = set()
        seen_source_facts: set[tuple[tuple[str, str, str], str]] = set()

        for card_id in ordered_ids:
            card = cards.get(card_id)
            if card is None:
                raise ValueError(f"candidate card disappeared: {card_id}")
            if card.card_kind == "navigation":
                if (
                    len(navigation_ids) < self.navigation_slots
                    and card.source_id not in navigation_sources
                ):
                    navigation_sources.add(card.source_id)
                    navigation_ids.append(card_id)
                continue
            identity = self._fact_identity(card)
            source_fact = (identity, card.source_id)
            if source_fact in seen_source_facts:
                continue
            seen_source_facts.add(source_fact)
            entry = (card_id, identity, card.source_id)
            if identity in primary_identities:
                alternates.setdefault(identity, []).append(entry)
            else:
                primary_identities.add(identity)
                primary.append(entry)

        reserved_navigation = min(len(navigation_ids), self.budget)
        fact_budget = self.budget - reserved_navigation
        alternate_identities = [
            identity for _, identity, _ in primary if identity in alternates
        ]
        alternate_slots = min(
            self.reserve_alternate_slots,
            len(alternate_identities),
            max(0, fact_budget - 1),
        )
        primary_slots = fact_budget - alternate_slots
        selected_entries = primary[:primary_slots]
        selected = [card_id for card_id, _, _ in selected_entries]
        selected_sources: dict[tuple[str, str, str], set[str]] = {}
        for _, identity, source_id in selected_entries:
            selected_sources.setdefault(identity, set()).add(source_id)

        alternates_added = 0
        for identity in alternate_identities:
            if identity not in selected_sources:
                continue
            for card_id, _, source_id in alternates[identity]:
                sources = selected_sources[identity]
                if source_id in sources or len(sources) >= self.max_sources_per_fact:
                    continue
                selected.append(card_id)
                sources.add(source_id)
                alternates_added += 1
                break
            if alternates_added >= alternate_slots:
                break

        # If a reserved slot could not be filled (for example because every
        # alternate came from an already selected source), return it to fact
        # diversity instead of shrinking the reranker input.
        for card_id, identity, source_id in primary[primary_slots:]:
            if len(selected) >= fact_budget:
                break
            selected.append(card_id)
            selected_sources.setdefault(identity, set()).add(source_id)

        selected.extend(navigation_ids[:reserved_navigation])
        return selected
