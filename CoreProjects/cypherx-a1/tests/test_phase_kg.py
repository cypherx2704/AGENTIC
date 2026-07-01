"""Phase KG — knowledge-graph accuracy: the reusable kg lib (pure, network-free).

Schema-guided extraction, type-aware coreference, and extraction QA (source span +
confidence) are pure logic factored into ``cypherx_a1.kg`` so a future shared service is a
lift-out. The DB-backed wiring (mention map, edge redirect, bitemporal as-of reads) is
exercised by scripts/live_graph_demo.py against a real Postgres; here we unit-test the
pure decision logic that the accuracy depends on."""

from __future__ import annotations

from cypherx_a1.kg import DEFAULT_SCHEMA, are_coreferent, mention_variants, normalize_mention
from cypherx_a1.kg.extraction import parse_extracted_edges
from cypherx_a1.kg.schema import GraphSchema, RelationRule

# ── Schema / ontology-guided extraction ──────────────────────────────────────────────────

_HALLUCINATED = (
    '{"edges":['
    '{"rel":"depends_on","target_kind":"service","target_key":"auth-service","confidence":0.9},'
    '{"rel":"likes","target_kind":"service","target_key":"payments-db","confidence":0.95},'
    '{"rel":"is_friends_with","target_kind":"person","target_key":"bob@acme.io","confidence":0.9}]}'
)


def test_default_schema_reproduces_todays_allowed_set() -> None:
    # The default schema's relations == the extractor's historical extractable rels.
    assert DEFAULT_SCHEMA.relations == frozenset(
        {"depends_on", "decided_in", "caused", "resolved", "expert_in", "mentions"}
    )
    assert DEFAULT_SCHEMA.validate(rel="depends_on", target_kind="service") is True
    assert DEFAULT_SCHEMA.validate(rel="likes", target_kind="service") is False


def test_schema_reject_mode_drops_off_schema_relations() -> None:
    parsed = parse_extracted_edges(
        _HALLUCINATED, schema=DEFAULT_SCHEMA, schema_mode="reject"
    )
    rels = {e.rel for e in parsed.edges}
    assert rels == {"depends_on"}  # 'likes'/'is_friends_with' rejected as off-schema
    assert parsed.rejected == 2


def test_schema_flag_mode_keeps_but_marks_off_schema() -> None:
    parsed = parse_extracted_edges(
        _HALLUCINATED, schema=DEFAULT_SCHEMA, schema_mode="flag"
    )
    by_rel = {e.rel: e for e in parsed.edges}
    assert by_rel["depends_on"].schema_ok is True
    assert by_rel["likes"].schema_ok is False
    assert by_rel["is_friends_with"].schema_ok is False
    assert parsed.rejected == 0  # nothing dropped in flag mode


def test_no_schema_is_unchanged_behavior() -> None:
    # With schema=None only the basic shape allow-list applies (today's behavior): off-schema
    # rels are not in the allow-list, so they are silently skipped (not counted as rejected).
    parsed = parse_extracted_edges(_HALLUCINATED)
    assert {e.rel for e in parsed.edges} == {"depends_on"}
    assert parsed.rejected == 0


def test_schema_source_kind_constraint() -> None:
    # A schema that only allows person -> service expert_in rejects a repo -> service one.
    schema = GraphSchema.from_rules(
        [RelationRule("expert_in", frozenset({"service"}), source_kinds=frozenset({"person"}))]
    )
    assert schema.validate(rel="expert_in", target_kind="service", source_kind="person") is True
    assert schema.validate(rel="expert_in", target_kind="service", source_kind="repo") is False


# ── Extraction QA: source span + confidence capture ───────────────────────────────────────

def test_source_span_captured_from_evidence_or_explicit() -> None:
    content = (
        '{"edges":['
        '{"rel":"depends_on","target_kind":"service","target_key":"x","confidence":0.9,'
        '"evidence":"calls x to verify"},'
        '{"rel":"caused","target_kind":"incident","target_key":"y","confidence":0.8,'
        '"source_span":"line 42: y broke"}]}'
    )
    parsed = parse_extracted_edges(content)
    by_key = {e.target_key: e for e in parsed.edges}
    assert by_key["x"].source_span == "calls x to verify"  # falls back to evidence
    assert by_key["y"].source_span == "line 42: y broke"   # explicit span field wins


def test_confidence_floor_drop_counts_rejected() -> None:
    content = (
        '{"edges":['
        '{"rel":"depends_on","target_kind":"service","target_key":"hi","confidence":0.9},'
        '{"rel":"depends_on","target_kind":"service","target_key":"lo","confidence":0.2}]}'
    )
    parsed = parse_extracted_edges(content, floor=0.6, mode="drop")
    assert [e.target_key for e in parsed.edges] == ["hi"]
    assert parsed.rejected == 1


# ── Type-aware coreference (entity resolution) ────────────────────────────────────────────

def test_person_coreference_initial_and_ordering() -> None:
    assert are_coreferent("J. Smith", "John Smith", kind="person") is True
    assert are_coreferent("John Smith", "Smith, John", kind="person") is True
    assert are_coreferent("Dr. John Smith", "John Smith", kind="person") is True
    assert are_coreferent("John Smith Jr.", "John Smith", kind="person") is True


def test_person_coreference_negatives() -> None:
    assert are_coreferent("John Smith", "Jane Smith", kind="person") is False
    assert are_coreferent("John Smith", "John Doe", kind="person") is False


def test_keyed_kinds_only_exact_match() -> None:
    assert are_coreferent("auth-service", "auth-service", kind="service") is True
    assert are_coreferent("auth-service", "auth-service-v2", kind="service") is False
    assert are_coreferent("acme/payments", "acme/payments-old", kind="repo") is False


def test_normalize_and_variants() -> None:
    assert normalize_mention("  Dr.  John  Smith ") == "dr john smith"
    variants = mention_variants("Smith, John", kind="person")
    assert "john smith" in variants  # reordered given/last form is a lookup variant
