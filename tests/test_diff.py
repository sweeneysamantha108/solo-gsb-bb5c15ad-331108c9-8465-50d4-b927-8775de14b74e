"""跨版本差异归并（decision_diff / list_diff / conflict_diff）单元测试。"""

from app.engine import compare_results, conflict_diff, decision_diff, list_diff


# ---------- 决策 JSON 路径差异 ----------


def test_decision_diff_added_removed_changed():
    base = {"a": 1, "b": {"x": 1}, "l": [1, 2]}
    target = {"b": {"x": 2, "y": 3}, "l": [1, 9], "c": 4}
    diff = decision_diff(base, target)
    assert diff["added"] == [
        {"path": "/b/y", "old": None, "new": 3},
        {"path": "/c", "old": None, "new": 4},
    ]
    assert diff["removed"] == [{"path": "/a", "old": 1, "new": None}]
    assert diff["changed"] == [
        {"path": "/b/x", "old": 1, "new": 2},
        {"path": "/l/1", "old": 2, "new": 9},
    ]


def test_decision_diff_nested_subtree_expanded_to_leaves():
    # 整棵新增/移除的子树逐叶展开
    assert decision_diff({"a": 1}, {"a": 1, "sub": {"k": {"j": [1, 2]}}})["added"] == [
        {"path": "/sub/k/j/0", "old": None, "new": 1},
        {"path": "/sub/k/j/1", "old": None, "new": 2},
    ]
    assert decision_diff({"sub": {"k": 1}}, {})["removed"] == [
        {"path": "/sub/k", "old": 1, "new": None}
    ]
    # 空容器占自身路径
    assert decision_diff({}, {"empty": []})["added"] == [
        {"path": "/empty", "old": None, "new": []}
    ]
    assert decision_diff({"empty": {}}, {})["removed"] == [
        {"path": "/empty", "old": {}, "new": None}
    ]


def test_decision_diff_root_containers_and_scalars():
    # 根上 dict 与 list 类型不同：直接记为根路径 changed
    diff = decision_diff({"a": 1}, [1, 2])
    assert diff["changed"] == [{"path": "", "old": {"a": 1}, "new": [1, 2]}]
    assert diff["added"] == [] and diff["removed"] == []

    # 同路径 dict 与标量类型不同
    diff = decision_diff({"a": {"k": 1}}, {"a": 5})
    assert diff["changed"] == [{"path": "/a", "old": {"k": 1}, "new": 5}]

    # 列表长度不同：按下标对齐，多出的元素 added/removed
    diff = decision_diff([1], [1, 2, 3])
    assert diff["changed"] == []
    assert diff["added"] == [
        {"path": "/1", "old": None, "new": 2},
        {"path": "/2", "old": None, "new": 3},
    ]


def test_decision_diff_bool_is_not_int():
    # JSON 语义下 true 与 1 是不同的标量
    diff = decision_diff({"f": True}, {"f": 1})
    assert diff["changed"] == [{"path": "/f", "old": True, "new": 1}]


def test_decision_diff_rfc6901_escaping_and_order():
    base = {"a/b": 1, "a~b": 2}
    target = {"a/b": 1, "a~b": 9, "z": 0}
    diff = decision_diff(base, target)
    assert diff["changed"] == [{"path": "/a~0b", "old": 2, "new": 9}]
    # / 转义为 ~1；各分类按指针字典序
    assert decision_diff({}, {"/": 1, "~": 1, "0": 1})["added"] == [
        {"path": "/0", "old": None, "new": 1},
        {"path": "/~0", "old": None, "new": 1},
        {"path": "/~1", "old": None, "new": 1},
    ]
    # 整数下标在路径中原样输出
    assert decision_diff({"items": [1]}, {"items": [2]})["changed"] == [
        {"path": "/items/0", "old": 1, "new": 2}
    ]


def test_decision_diff_equal_decisions_empty():
    decision = {"a": [1, 2], "b": {"c": {"d": True}}, "n": None}
    assert decision_diff(decision, decision) == {"added": [], "removed": [], "changed": []}
    assert decision_diff({}, {}) == {"added": [], "removed": [], "changed": []}


# ---------- 命中规则列表摘要 ----------


def test_list_diff_ordered_echo_and_set_delta():
    assert list_diff(["a", "b"], ["b", "c"]) == {
        "base": ["a", "b"],
        "target": ["b", "c"],
        "added": ["c"],
        "removed": ["a"],
    }
    assert list_diff([], []) == {"base": [], "target": [], "added": [], "removed": []}


# ---------- 冲突摘要 ----------


def test_conflict_diff_group_added_removed_changed():
    base = [
        {
            "group": "discount",
            "policy": "priority",
            "winner": "large",
            "suppressed": [{"rule_code": "vip", "reason": "lower_priority", "priority": 5}],
        },
        {"group": "coupon", "policy": "first_hit", "winner": "c1", "suppressed": []},
    ]
    target = [
        {
            "group": "discount",
            "policy": "priority",
            "winner": "vip",
            "suppressed": [{"rule_code": "large", "reason": "lower_priority", "priority": 10}],
        },
        {"group": "newgroup", "policy": "first_hit", "winner": "n", "suppressed": []},
    ]
    diff = conflict_diff(base, target)
    assert diff["base"] == base and diff["target"] == target
    assert [c["group"] for c in diff["added"]] == ["newgroup"]
    assert [c["group"] for c in diff["removed"]] == ["coupon"]
    changed = diff["changed"]
    assert len(changed) == 1
    assert changed[0]["group"] == "discount"
    assert changed[0]["field"] is None
    assert changed[0]["old"]["winner"] == "large"
    assert changed[0]["new"]["winner"] == "vip"


def test_conflict_diff_field_overwrite_entries():
    base = [
        {"group": None, "policy": "collect_all", "field": "x",
         "previous_winner": "r1", "winner": "r2", "reason": "field_overwritten"},
    ]
    target = [
        {"group": None, "policy": "collect_all", "field": "x",
         "previous_winner": "r1", "winner": "r3", "reason": "field_overwritten"},
        {"group": None, "policy": "collect_all", "field": "y",
         "previous_winner": "r1", "winner": "r3", "reason": "field_overwritten"},
    ]
    diff = conflict_diff(base, target)
    assert [c["field"] for c in diff["added"]] == ["y"]
    assert diff["removed"] == []
    assert diff["changed"] == [
        {"group": None, "field": "x", "old": base[0], "new": target[0]}
    ]


def test_conflict_diff_no_changes():
    conflicts = [
        {"group": "g", "policy": "priority", "winner": "w", "suppressed": []},
        {"group": None, "policy": "collect_all", "field": "f",
         "previous_winner": "a", "winner": "b", "reason": "field_overwritten"},
    ]
    diff = conflict_diff(conflicts, conflicts)
    assert diff["added"] == [] and diff["removed"] == [] and diff["changed"] == []


# ---------- compare_results 汇总 ----------


def test_compare_results_assembles_summary():
    base_result = {
        "fired_rule_codes": ["r1", "r2"],
        "hit_rule_codes": ["r1"],
        "conflicts": [
            {"group": "g", "policy": "priority", "winner": "r1",
             "suppressed": [{"rule_code": "r2", "reason": "lower_priority", "priority": 0}]},
        ],
    }
    target_result = {
        "fired_rule_codes": ["r1", "r3"],
        "hit_rule_codes": ["r1", "r3"],
        "conflicts": [],
    }
    summary = compare_results(base_result, target_result)
    assert summary["fired_rules"] == {
        "base": ["r1", "r2"], "target": ["r1", "r3"],
        "added": ["r3"], "removed": ["r2"],
    }
    assert summary["hit_rules"]["added"] == ["r3"]
    assert [c["group"] for c in summary["conflicts"]["removed"]] == ["g"]
