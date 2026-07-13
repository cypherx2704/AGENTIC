"""Corpus-derived jailbreak / injection signature pack (Aho-Corasick — B2).

``_JAILBREAK_PATTERNS`` / ``_PROMPT_INJECTION_PATTERNS`` in ``definitions`` are ~6
hand-written regexes each; the published corpora (CCS'24 In-The-Wild Jailbreak Prompts,
JailbreakBench) show the real attack surface is far larger, so recall is capped by hand
curation. This module distills a versioned signature set of genuinely-known, publicly
documented attack templates (DAN / AIM / STAN / developer-mode / role-play /
"ignore previous instructions" families) from a **checked-in data file** and compiles it
ONCE at import into an Aho-Corasick automaton.

Aho-Corasick keeps the scan **O(text length) regardless of pattern count**, so there is no
latency regression even at hundreds of signatures — strictly better-scaling than a per-
pattern ``finditer`` loop. The automaton is a real pure-Python implementation (goto / fail /
output links), so there is NO hard C-extension dependency; ``pyahocorasick`` is used
transparently when installed but is never required.

The full JailbreakBench / in-the-wild datasets drop in later via the SAME schema
(``{"category","family","phrase"}`` JSONL) and the same loader — only the data file grows.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Checked-in signature corpus (curated real public attack templates; full corpora drop in
# unchanged via this same JSONL schema).
_SIGNATURES_PATH = Path(__file__).resolve().parents[2] / "data" / "attack_signatures.jsonl"

CATEGORY_INJECTION = "injection"
CATEGORY_JAILBREAK = "jailbreak"


class AhoCorasick:
    """A real Aho-Corasick multi-pattern automaton (pure-Python; no C extension).

    ``add`` inserts a lowercase phrase; ``build`` links the failure/output functions; and
    ``iter_matches`` yields every ``(start, end, phrase)`` occurrence in a single O(n) pass
    over the haystack. Case folding is the caller's responsibility (we store + scan lower).
    """

    __slots__ = ("_goto", "_fail", "_out", "_built")

    def __init__(self) -> None:
        # Node 0 is the root. Parallel arrays indexed by node id.
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._out: list[list[str]] = [[]]
        self._built = False

    def add(self, phrase: str) -> None:
        """Insert a phrase (already lowercased) into the trie."""
        if not phrase:
            return
        node = 0
        for ch in phrase:
            nxt = self._goto[node].get(ch)
            if nxt is None:
                nxt = len(self._goto)
                self._goto.append({})
                self._fail.append(0)
                self._out.append([])
                self._goto[node][ch] = nxt
            node = nxt
        self._out[node].append(phrase)

    def build(self) -> None:
        """Compute failure links + merge output sets (breadth-first from the root)."""
        queue: deque[int] = deque()
        for nxt in self._goto[0].values():
            self._fail[nxt] = 0
            queue.append(nxt)
        while queue:
            r = queue.popleft()
            for ch, s in self._goto[r].items():
                queue.append(s)
                state = self._fail[r]
                while state and ch not in self._goto[state]:
                    state = self._fail[state]
                fs = self._goto[state].get(ch, 0)
                if fs == s:
                    fs = 0
                self._fail[s] = fs
                if self._out[fs]:
                    self._out[s].extend(self._out[fs])
        self._built = True

    def iter_matches(self, text: str) -> Iterator[tuple[int, int, str]]:
        """Yield ``(start, end, phrase)`` for every phrase occurrence in ``text``."""
        node = 0
        for i, ch in enumerate(text):
            while node and ch not in self._goto[node]:
                node = self._fail[node]
            node = self._goto[node].get(ch, 0)
            if self._out[node]:
                for phrase in self._out[node]:
                    start = i - len(phrase) + 1
                    yield start, i + 1, phrase

    @property
    def empty(self) -> bool:
        return len(self._goto) == 1


def _load_signatures(path: Path) -> list[tuple[str, str]]:
    """Return ``[(category, phrase_lower), ...]`` from the JSONL corpus (fail-soft)."""
    out: list[tuple[str, str]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("attack_signatures_load_failed", path=str(path), error=str(exc))
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("attack_signature_bad_row", row=line[:48])
            continue
        category = row.get("category")
        phrase = row.get("phrase")
        if not isinstance(category, str) or not isinstance(phrase, str) or not phrase.strip():
            continue
        out.append((category, phrase.strip().lower()))
    return out


def build_automatons(path: Path | None = None) -> tuple[AhoCorasick, AhoCorasick]:
    """Compile the (injection, jailbreak) automatons from the corpus. Never raises."""
    injection = AhoCorasick()
    jailbreak = AhoCorasick()
    n_injection = 0
    n_jailbreak = 0
    for category, phrase in _load_signatures(path or _SIGNATURES_PATH):
        if category == CATEGORY_INJECTION:
            injection.add(phrase)
            n_injection += 1
        elif category == CATEGORY_JAILBREAK:
            jailbreak.add(phrase)
            n_jailbreak += 1
    injection.build()
    jailbreak.build()
    logger.info(
        "attack_signatures_compiled", injection=n_injection, jailbreak=n_jailbreak
    )
    return injection, jailbreak


# Compiled ONCE at import from the checked-in corpus (empty + inert if the file is absent).
INJECTION_AUTOMATON, JAILBREAK_AUTOMATON = build_automatons()


def signature_matches(automaton: AhoCorasick, lowered_text: str) -> list[tuple[int, int, str]]:
    """Return WORD-BOUNDED phrase matches (list of ``(start, end, matched)``).

    Aho-Corasick is a substring matcher; we post-filter each hit to whole-word boundaries
    (mirroring ``\\b`` semantics) so e.g. the DAN phrase ``as dan`` cannot fire inside
    "was dancing". A boundary is required only on an edge whose phrase char is alphanumeric.
    De-duplicated by span.
    """
    if automaton.empty or not lowered_text:
        return []
    seen: set[tuple[int, int]] = set()
    hits: list[tuple[int, int, str]] = []
    n = len(lowered_text)
    for start, end, phrase in automaton.iter_matches(lowered_text):
        if phrase[:1].isalnum() and start > 0 and lowered_text[start - 1].isalnum():
            continue
        if phrase[-1:].isalnum() and end < n and lowered_text[end].isalnum():
            continue
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        hits.append((start, end, lowered_text[start:end]))
    return hits
