from app.engine import compile_ruleset, evaluate


def _rs(rules, policy="collect_all"):
    return compile_ruleset("t", "", policy, rules)


def test_simple_dependency_chain_and_order():
    rs = _rs([
        {"code": "a", "condition": 'doc["x"] >= 10', "action": '{"a": 1}'},
        {"code": "b", "condition": "a.hit", "depends_on": ["a"], "action": '{"b": 2}'},
        {"code": "c", "condition": "b.hit", "depends_on": ["b"], "action": '{"c": 3}'},
    ])
    result = evaluate(rs, {"x": 20})
    assert result["evaluation_order"] == ["a", "b", "c"]
    assert result["fired_rule_codes"] == ["a", "b", "c"]
    assert result["hit_rule_codes"] == ["a", "b", "c"]
    assert result["decision"] == {"a": 1, "b": 2, "c": 3}
    assert result["converged"] is True
    # 首次按拓扑序即全部点亮，第二轮确认稳定
    assert result["iterations"] == 2


def test_chain_stops_when_root_not_fired():
    rs = _rs([
        {"code": "a", "condition": 'doc["x"] >= 10'},
        {"code": "b", "condition": "a.hit", "depends_on": ["a"]},
    ])
    result = evaluate(rs, {"x": 1})
    assert result["fired_rule_codes"] == []
    assert result["decision"] == {}
    assert result["iterations"] == 1


def test_negative_dependency_converges():
    # c 命中要求 a 不命中。首轮 a=false -> c=true；a 随后仍不命中 -> 定点
    rs = _rs([
        {"code": "a", "condition": 'doc["x"] >= 100'},
        {
            "code": "c",
            "condition": "a.hit == false",
            "depends_on": ["a"],
            "action": '{"review": true}',
        },
    ])
    result = evaluate(rs, {"x": 1})
    assert result["fired_rule_codes"] == ["c"]
    assert result["hit_rule_codes"] == ["c"]
    assert result["decision"] == {"review": True}
    assert result["iterations"] == 2


def test_collect_all_merges_actions():
    rs = _rs([
        {"code": "a", "condition": "true", "action": '{"x": 1}'},
        {"code": "b", "condition": "true", "action": '{"y": 2}'},
    ])
    result = evaluate(rs, {})
    assert result["hit_rule_codes"] == ["a", "b"]
    assert result["decision"] == {"x": 1, "y": 2}


def test_collect_all_field_overwrite_explained():
    rs = _rs([
        {"code": "a", "condition": "true", "action": '{"x": 1}'},
        {"code": "b", "condition": "true", "action": '{"x": 2}'},
    ])
    result = evaluate(rs, {})
    assert result["decision"]["x"] == 2
    fc = [c for c in result["conflicts"] if c.get("reason") == "field_overwritten"]
    assert len(fc) == 1
    assert fc[0]["previous_winner"] == "a"
    assert fc[0]["winner"] == "b"


def test_priority_policy_keeps_highest():
    rs = _rs([
        {
            "code": "low",
            "condition": "true",
            "priority": 5,
            "conflict_group": "g",
            "conflict_policy": "priority",
            "action": '{"discount": 0.05}',
        },
        {
            "code": "high",
            "condition": "true",
            "priority": 20,
            "conflict_group": "g",
            "conflict_policy": "priority",
            "action": '{"discount": 0.2}',
        },
    ])
    result = evaluate(rs, {})
    assert result["hit_rule_codes"] == ["high"]
    assert result["decision"] == {"discount": 0.2}
    conflict = result["conflicts"][0]
    assert conflict["policy"] == "priority"
    assert conflict["winner"] == "high"
    assert conflict["suppressed"][0]["rule_code"] == "low"
    assert conflict["suppressed"][0]["reason"] == "lower_priority"


def test_first_hit_policy_keeps_earliest_in_topo_order():
    # 提交顺序 c1 在前；即使无依赖，拓扑序保持提交顺序
    rs = _rs([
        {
            "code": "c1",
            "condition": "true",
            "conflict_group": "g",
            "conflict_policy": "first_hit",
            "action": '{"coupon": "C1"}',
        },
        {
            "code": "c2",
            "condition": "true",
            "conflict_group": "g",
            "conflict_policy": "first_hit",
            "action": '{"coupon": "C2"}',
        },
    ])
    result = evaluate(rs, {})
    assert result["hit_rule_codes"] == ["c1"]
    assert result["decision"] == {"coupon": "C1"}
    assert result["conflicts"][0]["winner"] == "c1"
    assert result["conflicts"][0]["suppressed"][0]["rule_code"] == "c2"
    assert result["conflicts"][0]["suppressed"][0]["reason"] == "not_first_hit"


def test_groups_are_independent():
    rs = _rs([
        {
            "code": "g1a", "condition": "true",
            "conflict_group": "g1", "conflict_policy": "priority",
            "priority": 1, "action": '{"a": 1}',
        },
        {
            "code": "g1b", "condition": "true",
            "conflict_group": "g1", "conflict_policy": "priority",
            "priority": 2, "action": '{"a": 2}',
        },
        {
            "code": "free", "condition": "true", "action": '{"b": 9}',
        },
    ])
    result = evaluate(rs, {})
    assert result["hit_rule_codes"] == ["g1b", "free"]
    assert result["decision"] == {"a": 2, "b": 9}


def test_default_policy_inherited_from_ruleset():
    rs = _rs([
        {"code": "a", "condition": "true", "conflict_group": "g",
         "action": '{"x": 1}'},
        {"code": "b", "condition": "true", "conflict_group": "g",
         "action": '{"y": 2}'},
    ], policy="collect_all")
    result = evaluate(rs, {})
    assert set(result["hit_rule_codes"]) == {"a", "b"}


def test_non_dict_action_collected_in_results():
    rs = _rs([
        {"code": "a", "condition": "true", "action": "42"},
        {"code": "b", "condition": "true", "action": '"ok"'},
    ])
    result = evaluate(rs, {})
    assert result["decision"]["results"] == [
        {"rule_code": "a", "value": 42},
        {"rule_code": "b", "value": "ok"},
    ]


def test_rule_value_reference_in_action():
    rs = _rs([
        {"code": "base", "condition": "true", "action": "100"},
        {
            "code": "boost",
            "condition": "base.hit",
            "depends_on": ["base"],
            "action": "base.value * 2",
        },
    ])
    result = evaluate(rs, {})
    assert result["decision"]["results"] == [
        {"rule_code": "base", "value": 100},
        {"rule_code": "boost", "value": 200},
    ]


def test_trace_carries_errors_and_marks_rule_not_fired():
    rs = _rs([
        # 对 None 做非法算术，运行期 EvaluationError -> 条件不成立
        {"code": "bad", "condition": 'doc["missing"] + 1 > 0'},
    ])
    result = evaluate(rs, {})
    trace = result["traces"][0]
    assert trace["rule_code"] == "bad"
    assert trace["hit"] is False
    assert "error" in trace


def test_no_action_rule_emits_no_decision():
    rs = _rs([{"code": "gate", "condition": "true"}])
    result = evaluate(rs, {})
    assert result["fired_rule_codes"] == ["gate"]
    assert result["decision"] == {}
