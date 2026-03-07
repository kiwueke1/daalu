# tests/bootstrap/test_values.py
# Tests for daalu.bootstrap.engine.values.deep_merge

import pytest
from daalu.bootstrap.engine.values import deep_merge


def test_deep_merge_scalar_b_overwrites_a():
    a = {"key": "old"}
    b = {"key": "new"}
    result = deep_merge(a, b)
    assert result == {"key": "new"}


def test_deep_merge_nested_dicts_merged_recursively():
    a = {"outer": {"x": 1, "y": 2}}
    b = {"outer": {"y": 99, "z": 3}}
    result = deep_merge(a, b)
    assert result == {"outer": {"x": 1, "y": 99, "z": 3}}


def test_deep_merge_list_in_b_overwrites_list_in_a():
    a = {"items": [1, 2, 3]}
    b = {"items": [4, 5]}
    result = deep_merge(a, b)
    assert result == {"items": [4, 5]}


def test_deep_merge_empty_b_returns_copy_of_a():
    a = {"x": 1, "y": {"z": 2}}
    result = deep_merge(a, {})
    assert result == {"x": 1, "y": {"z": 2}}


def test_deep_merge_empty_a_returns_copy_of_b():
    b = {"x": 1}
    result = deep_merge({}, b)
    assert result == {"x": 1}


def test_deep_merge_does_not_mutate_a():
    a = {"outer": {"x": 1}}
    b = {"outer": {"y": 2}}
    deep_merge(a, b)
    assert a == {"outer": {"x": 1}}


def test_deep_merge_does_not_mutate_b():
    a = {"k": 1}
    b = {"k": 2}
    deep_merge(a, b)
    assert b == {"k": 2}


def test_deep_merge_deep_nesting():
    a = {"l1": {"l2": {"l3": {"val": "a", "keep": True}}}}
    b = {"l1": {"l2": {"l3": {"val": "b"}}}}
    result = deep_merge(a, b)
    assert result == {"l1": {"l2": {"l3": {"val": "b", "keep": True}}}}


def test_deep_merge_b_adds_new_top_level_key():
    a = {"existing": 1}
    b = {"new": 2}
    result = deep_merge(a, b)
    assert result == {"existing": 1, "new": 2}


def test_deep_merge_scalar_overrides_nested_dict_in_a():
    # b wins even when types mismatch
    a = {"key": {"nested": True}}
    b = {"key": "scalar"}
    result = deep_merge(a, b)
    assert result == {"key": "scalar"}


def test_deep_merge_nested_dict_overrides_scalar_in_a():
    a = {"key": "scalar"}
    b = {"key": {"nested": True}}
    result = deep_merge(a, b)
    assert result == {"key": {"nested": True}}


def test_deep_merge_none_value_in_b_overwrites():
    a = {"key": "value"}
    b = {"key": None}
    result = deep_merge(a, b)
    assert result["key"] is None


def test_deep_merge_both_empty():
    assert deep_merge({}, {}) == {}
