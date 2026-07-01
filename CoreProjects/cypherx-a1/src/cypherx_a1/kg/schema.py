"""Schema / ontology for schema-guided extraction (pure, no I/O).

Constrains the LLM extractor to an allowed relation + entity-type set. A relation is only
admitted if (a) the relation name is in the ontology, (b) the target entity kind is allowed,
and (c) — when the rule declares them — the relation is allowed between the source and target
kinds. Anything outside the schema is rejected (or flagged), which cuts hallucinated
relations from entering the crown-jewel graph. Grounded in schema-guided / ontology-
constrained extraction (vs. open IE).

The :data:`DEFAULT_SCHEMA` reproduces TODAY's extractor behavior exactly: the same
extractable relations and target kinds the extractor already allowed, with permissive
source-kind rules (so enabling schema validation with the default schema changes nothing).
A deployment can tighten the schema via config without code changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The full entity-kind vocabulary the graph supports (matches the entities_kind_enum CHECK
# across the init + Phase B migrations). Schema validation may use a subset of this.
ALL_ENTITY_KINDS: frozenset[str] = frozenset(
    {
        "person", "service", "repo", "feature", "decision", "incident", "pr", "ticket",
        "document", "change", "capability", "expertise_summary",
    }
)


@dataclass(frozen=True)
class RelationRule:
    """An allowed relation in the ontology.

    ``target_kinds`` is the set of entity kinds the relation may point AT (the dst). If
    ``source_kinds`` is empty the relation may originate from any kind (permissive — today's
    behavior); otherwise the src entity's kind must be in the set.
    """

    rel: str
    target_kinds: frozenset[str]
    source_kinds: frozenset[str] = frozenset()

    def allows(self, *, target_kind: str, source_kind: str | None = None) -> bool:
        if target_kind not in self.target_kinds:
            return False
        return not (
            self.source_kinds and source_kind is not None and source_kind not in self.source_kinds
        )


@dataclass(frozen=True)
class GraphSchema:
    """An ontology: the relations an extractor may emit + their type constraints."""

    rules: dict[str, RelationRule] = field(default_factory=dict)

    @property
    def relations(self) -> frozenset[str]:
        return frozenset(self.rules)

    def target_kinds(self) -> frozenset[str]:
        out: set[str] = set()
        for r in self.rules.values():
            out |= set(r.target_kinds)
        return frozenset(out)

    def validate(
        self, *, rel: str, target_kind: str, source_kind: str | None = None
    ) -> bool:
        """True iff ``rel`` is in the schema and admits ``target_kind`` (and ``source_kind``
        when the rule constrains it). The single decision schema-guided extraction is built
        on — used to reject/flag out-of-schema (hallucinated) relations."""
        rule = self.rules.get(rel)
        if rule is None:
            return False
        return rule.allows(target_kind=target_kind, source_kind=source_kind)

    @classmethod
    def from_rules(cls, rules: list[RelationRule]) -> GraphSchema:
        return cls(rules={r.rel: r for r in rules})


# The extractable target kinds the extractor already allowed (extractor._TARGET_KINDS).
_EXTRACT_TARGETS = frozenset(
    {"service", "repo", "feature", "decision", "incident", "person", "document", "pr", "ticket"}
)

# DEFAULT schema = today's allowed (rel, target_kind) set, with permissive source kinds, so
# enabling schema validation with this schema is a no-op on current extraction output.
DEFAULT_SCHEMA = GraphSchema.from_rules(
    [
        RelationRule("depends_on", _EXTRACT_TARGETS),
        RelationRule("decided_in", _EXTRACT_TARGETS),
        RelationRule("caused", _EXTRACT_TARGETS),
        RelationRule("resolved", _EXTRACT_TARGETS),
        RelationRule("expert_in", _EXTRACT_TARGETS),
        RelationRule("mentions", _EXTRACT_TARGETS),
    ]
)
