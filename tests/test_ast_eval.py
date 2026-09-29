import pytest

from app.engine.ast_eval import (
    CompileError,
    EvaluationError,
    RuleRef,
    referenced_names,
    safe_eval,
)


def test_basic_comparison_and_boolean():
    doc = {"amount": 150, "region": "north", "tags": ["a", "b"]}
    ns = {"doc": doc}
    assert safe_eval('doc["amount"] >= 100 and doc["region"] == "north"', ns)
    assert safe_eval('doc["amount"] < 100 or doc["region"] == "north"', ns)
    assert safe_eval('not (doc["amount"] < 100)', ns)
    assert safe_eval('"a" in doc["tags"]', ns)
    assert safe_eval('contains(doc["tags"], "b")', ns)
    assert safe_eval('100 <= doc["amount"] <= 200', ns)


def test_missing_field_is_none():
    ns = {"doc": {"a": 1}}
    assert safe_eval('doc["missing"]', ns) is None
    assert safe_eval('doc["missing"]["deep"]', ns) is None
    assert safe_eval('doc["missing"] == None', ns)
    assert safe_eval('doc["missing"] >= 10', ns) is False


def test_rule_refs():
    ns = {"is_vip": RuleRef(True, {"discount": 0.15}), "doc": {}}
    assert safe_eval("is_vip.hit", ns) is True
    assert safe_eval('is_vip.value["discount"] == 0.15', ns)
    with pytest.raises(EvaluationError):
        safe_eval("is_vip.foo", ns)


def test_dsl_functions_and_strings():
    ns = {"doc": {"name": "ALICE"}}
    assert safe_eval('lower(doc["name"]) == "alice"', ns)
    assert safe_eval('doc["name"].lower() == "alice"', ns)
    assert safe_eval('doc["name"].endswith("ICE")', ns)
    assert safe_eval('len(doc["name"]) == 5', ns)
    assert safe_eval('round(3.14159, 2) == 3.14', ns)
    assert safe_eval('max(1, 2, 3) == 3', ns)


def test_dict_dot_access():
    ns = {"doc": {"customer": {"vip": True}}}
    assert safe_eval("doc.customer.vip == true", ns)


def test_sandbox_blocks_dunder_and_import():
    with pytest.raises(CompileError):
        safe_eval("doc.__class__", {"doc": {}})
    with pytest.raises(CompileError):
        safe_eval("__import__('os')", {})
    with pytest.raises(CompileError):
        safe_eval("lambda x: x", {})
    with pytest.raises(CompileError):
        safe_eval("[x for x in (1,2)]", {})
    with pytest.raises(CompileError):
        safe_eval("(1).__class__.__bases__", {})


def test_sandbox_blocks_arbitrary_calls():
    with pytest.raises(CompileError):
        safe_eval("doc.keys()", {"doc": {}})  # dict 方法不在白名单


def test_arithmetic():
    ns = {"doc": {"a": 5, "b": 2}}
    assert safe_eval('doc["a"] + doc["b"]', ns) == 7
    assert safe_eval('doc["a"] / doc["b"]', ns) == 2.5
    assert safe_eval('doc["a"] // doc["b"]', ns) == 2
    assert safe_eval('doc["a"] % doc["b"]', ns) == 1
    with pytest.raises(EvaluationError):
        safe_eval('1 / 0', ns)


def test_referenced_names():
    names = referenced_names('is_vip.hit and doc["x"] >= len(doc["y"])')
    assert names == {"is_vip", "doc"}


def test_ternary_and_literals():
    ns = {"doc": {"v": 3}}
    assert safe_eval('"big" if doc["v"] >= 3 else "small"', ns) == "big"
    assert safe_eval("[1, 2, 3][0]", ns) == 1
    assert safe_eval('{"a": 1}["a"]', ns) == 1
