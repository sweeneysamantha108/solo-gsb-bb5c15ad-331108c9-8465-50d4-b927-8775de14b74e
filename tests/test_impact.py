"""静态影响分析：反向依赖闭包、层级、冲突组联动。"""

import pytest

from app.engine import compile_ruleset, impact_analysis
from app.engine.impact import ImpactAnalysisError, reverse_dependencies


def _rule(code, condition="", depends_on=None, **kw):
    return {"code": code, "condition": condition, "depends_on": depends_on or [], **kw}


def _chain():
    # a <- b <- d
    # a <- c <- d （d 同时依赖 b、c，BFS 最短层级为 2）
    return compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            _rule("a"),
            _rule("b", "a.hit", ["a"]),
            _rule("c", "a.hit", ["a"]),
            _rule("d", "b.hit and c.hit", ["b", "c"]),
        ],
    )


def test_reverse_dependencies_includes_expression_references():
    compiled = _chain()
    assert compiled.evaluation_order == ["a", "b", "c", "d"]
    reverse = reverse_dependencies(compiled)
    assert reverse["a"] == ["b", "c"]
    assert reverse["b"] == ["d"]
    assert reverse["c"] == ["d"]
    assert reverse["d"] == []


def test_input_only_rule_has_empty_impact():
    compiled = _chain()
    result = impact_analysis(compiled, ["d"])
    assert result["rule_codes"] == ["d"]
    assert [r["rule_code"] for r in result["affected"]] == ["d"]
    assert result["affected"][0] == {
        "rule_code": "d",
        "level": 0,
        "relation": "input",
    }
    assert result["direct_dependents"] == []
    assert result["transitive_dependents"] == []
    assert result["coupled_rules"] == []


def test_direct_and_transitive_closure_with_levels():
    compiled = _chain()
    result = impact_analysis(compiled, ["a"])
    assert result["direct_dependents"] == ["b", "c"]
    assert result["transitive_dependents"] == ["d"]
    assert [r["rule_code"] for r in result["affected"]] == ["a", "b", "c", "d"]
    levels = {r["rule_code"]: r["level"] for r in result["affected"]}
    assert levels == {"a": 0, "b": 1, "c": 1, "d": 2}
    relations = {r["rule_code"]: r["relation"] for r in result["affected"]}
    assert relations == {
        "a": "input",
        "b": "direct_dependent",
        "c": "direct_dependent",
        "d": "transitive_dependent",
    }


def test_shortest_level_used_when_reachable_via_multiple_paths():
    # d -> b -> a 与 d -> c -> a 深度相同；构造 e 依赖 a 与 d，
    # 经 a 直接到达为 level 1，而非经 d 的 level 3
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            _rule("a"),
            _rule("b", "a.hit", ["a"]),
            _rule("c", "a.hit", ["a"]),
            _rule("d", "b.hit and c.hit", ["b", "c"]),
            _rule("e", "a.hit or d.hit", ["a", "d"]),
        ],
    )
    result = impact_analysis(compiled, ["a"])
    levels = {r["rule_code"]: r["level"] for r in result["affected"]}
    assert levels["e"] == 1
    assert "e" in result["direct_dependents"]
    assert "e" not in result["transitive_dependents"]


def test_multiple_inputs_dedup_and_merge():
    compiled = _chain()
    result = impact_analysis(compiled, ["b", "b", "c"])
    assert result["rule_codes"] == ["b", "c"]
    inputs = {r["rule_code"] for r in result["affected"] if r["relation"] == "input"}
    assert inputs == {"b", "c"}
    # d 直接依赖 b、c，属于 direct 而非 transitive
    assert result["direct_dependents"] == ["d"]
    assert result["transitive_dependents"] == []


def test_conflict_group_coupling():
    # discount 组：large(输入) 与 vip；coupon 组：c1、c2 与闭包无关
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            _rule("large", conflict_group="discount", conflict_policy="priority"),
            _rule("vip", conflict_group="discount", conflict_policy="priority"),
            _rule("c1", conflict_group="coupon", conflict_policy="first_hit"),
            _rule("c2", conflict_group="coupon", conflict_policy="first_hit"),
        ],
    )
    result = impact_analysis(compiled, ["large"])
    coupled = result["coupled_rules"]
    assert [c["rule_code"] for c in coupled] == ["vip"]
    assert coupled[0]["conflict_group"] == "discount"
    assert coupled[0]["coupled_with"] == "large"
    assert coupled[0]["level"] is None
    # coupon 组与闭包无任何交集，不产生联动
    assert result["direct_dependents"] == []
    assert result["transitive_dependents"] == []


def test_coupled_rules_not_duplicated_as_dependents():
    # peer 与输入同组，同时也被另一条依赖边波及；依赖分类优先
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            _rule("a", conflict_group="g", conflict_policy="priority"),
            _rule(
                "peer",
                "a.hit",
                ["a"],
                conflict_group="g",
                conflict_policy="priority",
            ),
        ],
    )
    result = impact_analysis(compiled, ["a"])
    assert result["direct_dependents"] == ["peer"]
    assert result["coupled_rules"] == []


def test_coupling_propagates_from_transitive_dependents():
    # g 组中输入 a 无组员，但其传递依赖方 d 与 peer 同组
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            _rule("a"),
            _rule("b", "a.hit", ["a"]),
            _rule("d", "b.hit", ["b"], conflict_group="g"),
            _rule("peer", conflict_group="g"),
        ],
    )
    result = impact_analysis(compiled, ["a"])
    assert result["direct_dependents"] == ["b"]
    assert result["transitive_dependents"] == ["d"]
    coupled = result["coupled_rules"]
    assert [c["rule_code"] for c in coupled] == ["peer"]
    assert coupled[0]["coupled_with"] == "d"
    # 联动关系不继续传播：peer 上没有任何依赖分析
    assert all(r["rule_code"] != "peer" for r in result["affected"])


def test_unknown_rule_code_raises():
    compiled = _chain()
    with pytest.raises(ImpactAnalysisError) as exc:
        impact_analysis(compiled, ["a", "nope"])
    assert exc.value.code == "RULE_NOT_FOUND"
    assert exc.value.details == {"rule_codes": ["nope"]}


def test_demo_ruleset_reverse_closure():
    # is_vip <- vip_discount <- manual_review；
    # 同组 coupon_save20 与闭包无关；discount 组 large_order 联动
    from app.seed import DEMO_PAYLOAD

    compiled = compile_ruleset(
        DEMO_PAYLOAD["name"],
        DEMO_PAYLOAD["description"],
        DEMO_PAYLOAD["default_conflict_policy"],
        DEMO_PAYLOAD["rules"],
    )
    result = impact_analysis(compiled, ["is_vip"])
    assert result["direct_dependents"] == ["vip_discount"]
    assert result["transitive_dependents"] == ["manual_review"]
    codes = [c["rule_code"] for c in result["coupled_rules"]]
    assert codes == ["large_order"]
    coupled = result["coupled_rules"][0]
    assert coupled["conflict_group"] == "discount"
    assert coupled["coupled_with"] == "vip_discount"
