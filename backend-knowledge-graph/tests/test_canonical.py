"""Canonical-value contract (M1/M2): reject anything that would make the digest
nondeterministic or ambiguous, so 'equal canonical bytes' == 'equal value'."""

from __future__ import annotations

import pytest

from bkg.protocol.canonical import canonical_bytes


def test_accepts_canonical_value() -> None:
    assert canonical_bytes({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'


def test_rejects_float() -> None:
    with pytest.raises(TypeError):
        canonical_bytes({"x": 1.5})


def test_rejects_nan_and_inf() -> None:
    with pytest.raises(TypeError):
        canonical_bytes(float("nan"))
    with pytest.raises(TypeError):
        canonical_bytes([float("inf")])


def test_rejects_tuple_so_it_cannot_alias_a_list() -> None:
    with pytest.raises(TypeError):
        canonical_bytes(("a", "b"))


def test_rejects_non_string_dict_keys() -> None:
    with pytest.raises(TypeError):
        canonical_bytes({1: "a"})


def test_rejects_bytes() -> None:
    with pytest.raises(TypeError):
        canonical_bytes({"x": b"raw"})
