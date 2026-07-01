"""Type-aware coreference helpers for entity resolution (pure, no I/O).

Decides whether two surface forms of the SAME entity kind co-refer — e.g. the person
mentions ``'J. Smith'`` and ``'John Smith'`` map to one canonical person. Coreference is
deliberately conservative and TYPE-AWARE: the matching rules differ by entity kind so we do
not over-merge (LINK-KG: coreference is the dominant source of KG error, but a wrong merge
is worse than a missed one).

Rules of thumb encoded here:
  * person — name normalization + initial/last-name compatibility ('J. Smith' ~ 'John Smith',
    'John Smith' ~ 'Smith, John'); an exact email/login is an exact match handled upstream.
  * repo / service / other code artifacts — identity is a stable key (e.g. 'owner/name'); we
    only collapse exact normalized equality (NEVER fuzzy — 'auth-service' must not merge with
    'auth-service-v2'), so these are safe by construction.

No DB, no settings, no network — the app's DB-backed resolver calls these to decide a merge.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[.,]")
# Honorifics / suffixes stripped from person names before comparison.
_HONORIFICS = frozenset({"mr", "mrs", "ms", "dr", "prof", "sir", "madam"})
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})

# Entity kinds whose identity is a STABLE KEY — resolve only on exact normalized equality.
_KEYED_KINDS = frozenset(
    {"repo", "service", "pr", "ticket", "feature", "decision", "incident", "document", "change"}
)


def normalize_mention(surface_form: str) -> str:
    """A stable normalized form for exact-match lookup: lowercased, punctuation-stripped,
    whitespace-collapsed. The ``entity_mentions.normalized_form`` key is built from this."""
    s = _PUNCT.sub(" ", surface_form.strip().lower())
    return _WS.sub(" ", s).strip()


def _name_tokens(surface_form: str) -> list[str]:
    """Tokenize a person name: handle 'Last, First' ordering, drop honorifics/suffixes."""
    s = surface_form.strip().lower()
    if "," in s:  # 'Smith, John' -> 'John Smith'
        last, _, first = s.partition(",")
        s = f"{first.strip()} {last.strip()}"
    toks = [_PUNCT.sub("", t) for t in _WS.split(s) if t]
    return [t for t in toks if t and t not in _HONORIFICS and t not in _SUFFIXES]


def _initials_compatible(a: list[str], b: list[str]) -> bool:
    """Each shorter-side given token must be compatible with the same-position longer-side
    token: equal, or one is an initial of the other ('j' ~ 'john')."""
    for x, y in zip(a, b, strict=False):
        if x == y:
            continue
        if len(x) == 1 and y.startswith(x):
            continue
        if len(y) == 1 and x.startswith(y):
            continue
        return False
    return True


def mention_variants(surface_form: str, *, kind: str) -> set[str]:
    """Normalized variants a mention could also be stored/looked-up under. For a person this
    includes the reordered 'first last' form; for keyed kinds it's just the normalized form."""
    variants = {normalize_mention(surface_form)}
    if kind == "person":
        toks = _name_tokens(surface_form)
        if toks:
            variants.add(" ".join(toks))
    return {v for v in variants if v}


def are_coreferent(a: str, b: str, *, kind: str) -> bool:
    """True iff two same-kind surface forms refer to the same entity.

    * Exact normalized equality always co-refers.
    * Keyed kinds (repo/service/…): ONLY exact normalized equality (never fuzzy).
    * person: last names must match and the given-name sequence must be initial-compatible,
      so 'J. Smith' ~ 'John Smith' but 'John Smith' !~ 'Jane Smith'.
    """
    na, nb = normalize_mention(a), normalize_mention(b)
    if na == nb:
        return True
    if kind in _KEYED_KINDS or kind != "person":
        return False
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    if ta[-1] != tb[-1]:  # last name must match
        return False
    short, long = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return _initials_compatible(short, long)
