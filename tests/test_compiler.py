import pytest

from app.engine import compile_ruleset
from app.engine.compiler import RulesetCompileError, detect_cycle


def _rule(code, condition="", depends_on=None, **kw):
    return {"code": code, "condition": condition, "depends_on": depends_on or [], **kw}


def test_topo_order_respects_dependencies():
    compiled = compile_ruleset(
        "t", "", "collect_all",
        [
            _rule("a", "b.hit", ["b"]),
            _rule("b", "c.hit", ["c"]),
            _rule("c"),
        ],
    )
    assert compiled.evaluation_order == ["c", "b", "a"]


def test_self_dependency_rejected():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset("t", "", "collect_all", [_rule("a", "a.hit", ["a"])])
    assert exc.value.code == "SELF_DEPENDENCY"


def test_direct_cycle_rejected():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset(
            "t", "", "collect_all",
            [
                _rule("a", "b.hit", ["b"]),
                _rule("b", "a.hit == true", ["a"]),
            ],
        )
    assert exc.value.code == "CYCLIC_DEPENDENCY"
    cycle = exc.value.details["cycle"]
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"a", "b"}


def test_indirect_cycle_rejected():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset(
            "t", "", "collect_all",
            [
                _rule("a", "b.hit", ["b"]),
                _rule("b", "c.hit", ["c"]),
                _rule("c", "a.hit", ["a"]),
            ],
        )
    assert exc.value.code == "CYCLIC_DEPENDENCY"
    assert len(exc.value.details["cycle"]) == 4


def test_detect_cycle_helper():
    compiled = compile_ruleset("t", "", "collect_all", [_rule("a"), _rule("b", "a.hit", ["a"])])
    assert detect_cycle(compiled.by_code) is None


def test_duplicate_rule_code():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset(
            "t", "", "collect_all", [_rule("a"), _rule("a")]
        )
    assert exc.value.code == "DUPLICATE_RULE_CODE"


def test_unknown_dependency():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset("t", "", "collect_all", [_rule("a", "b.hit", ["b"])])
    assert exc.value.code == "UNKNOWN_DEPENDENCY"


def test_undeclared_reference():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset(
            "t", "", "collect_all",
            [_rule("a", "b.hit", []), _rule("b")],
        )
    assert exc.value.code == "UNDECLARED_REFERENCE"
    assert exc.value.details["references"] == ["b"]


def test_invalid_expression_construction():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset(
            "t", "", "collect_all", [_rule("a", "__import__('os')")]
        )
    assert exc.value.code == "INVALID_EXPRESSION"


def test_invalid_rule_code():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset("t", "", "collect_all", [_rule("1bad")])
    assert exc.value.code == "INVALID_RULE_CODE"


def test_empty_ruleset_rejected():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset("t", "", "collect_all", [])
    assert exc.value.code == "EMPTY_RULESET"


def test_group_policy_conflict_rejected():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset(
            "t", "", "collect_all",
            [
                _rule("a", conflict_group="g", conflict_policy="priority"),
                _rule("b", conflict_group="g", conflict_policy="first_hit"),
            ],
        )
    assert exc.value.code == "CONFLICTING_GROUP_POLICY"


def test_invalid_policy():
    with pytest.raises(RulesetCompileError) as exc:
        compile_ruleset(
            "t", "", "weird", [_rule("a")]
        )
    assert exc.value.code == "INVALID_POLICY"


def test_depends_on_unused_is_ok():
    # 声明了依赖但条件未引用是允许的（用于强制计算顺序）
    compiled = compile_ruleset(
        "t", "", "collect_all", [_rule("a"), _rule("b", depends_on=["a"])]
    )
    assert compiled.evaluation_order == ["a", "b"]
