"""POST /api/rulesets/{id}/coverage 接口测试。

覆盖：覆盖单元构造（rule/group/field）、命中汇总与未命中规则、
冲突场景去重与观察下标、确定性贪心代表选择、失败项隔离且不参与选择、
不落库、404 与 422。
"""

import pytest


def _create_group_ruleset(client, ruleset_id="cov-groups"):
    return client.post(
        "/api/rulesets",
        json={
            "id": ruleset_id,
            "name": "cov-groups",
            "default_conflict_policy": "collect_all",
            "rules": [
                {
                    "code": "r_a",
                    "conflict_group": "g",
                    "conflict_policy": "priority",
                    "priority": 1,
                    "condition": 'doc["x"] >= 1',
                    "action": '{"v": "a"}',
                },
                {
                    "code": "r_b",
                    "conflict_group": "g",
                    "conflict_policy": "priority",
                    "priority": 2,
                    "condition": 'doc["x"] >= 2',
                    "action": '{"v": "b"}',
                },
                {
                    "code": "r_c",
                    "condition": 'doc["y"] == true',
                    "action": '{"z": 1}',
                },
            ],
        },
    ).json()["ruleset_id"]


def _unit_keys(items_or_scenarios):
    """把覆盖单元压成便于断言的身份元组集合。"""
    out = set()
    for u in items_or_scenarios:
        kind = u["kind"]
        if kind == "rule":
            out.add(("rule", u["rule_code"]))
        elif kind == "group":
            out.add(
                (
                    "group",
                    u["group"],
                    u["winner"],
                    tuple(u["suppressed_rule_codes"]),
                )
            )
        else:
            out.add(
                (
                    "field",
                    u["field"],
                    u["previous_winner"],
                    u["winner"],
                )
            )
    return out


def test_coverage_group_conflicts_units_counts_and_scenarios(client):
    rid = _create_group_ruleset(client)
    resp = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={
            "version": 1,
            "documents": [
                {"x": 1},  # 0: 仅 r_a，组内单成员裁决 r_a 胜出、无被压制
                {"x": 2},  # 1: r_a+r_b 命中，r_b 凭优先级胜出、压制 r_a
                {"y": True},  # 2: 仅 r_c
                {"x": 2, "y": True},  # 3: 三条全中 + r_b 压制 r_a
                {"x": 1},  # 4: 与 0 相同的场景，重复观察
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["ruleset_id"] == rid
    assert body["version"] == 1
    assert body["total"] == 5
    assert body["success_count"] == 5
    assert body["error_count"] == 0

    # 命中统计：按 evaluation_order 排列；r_a 在 0/1/3/4 命中
    assert body["rule_coverage"] == [
        {"code": "r_a", "hit_count": 4, "covered_by_indexes": [0, 1, 3, 4]},
        {"code": "r_b", "hit_count": 2, "covered_by_indexes": [1, 3]},
        {"code": "r_c", "hit_count": 2, "covered_by_indexes": [2, 3]},
    ]
    assert body["uncovered_rule_codes"] == []

    # 逐项覆盖单元：rule 单元 + group 单元（组成员即使单人也产生裁决单元）
    assert _unit_keys(body["items"][0]["coverage_units"]) == {
        ("rule", "r_a"),
        ("group", "g", "r_a", ()),
    }
    assert _unit_keys(body["items"][1]["coverage_units"]) == {
        ("rule", "r_a"),
        ("rule", "r_b"),
        ("group", "g", "r_b", ("r_a",)),
    }
    assert _unit_keys(body["items"][3]["coverage_units"]) == {
        ("rule", "r_a"),
        ("rule", "r_b"),
        ("rule", "r_c"),
        ("group", "g", "r_b", ("r_a",)),
    }

    # 冲突场景按首次观察顺序去重；observed_at_indexes 升序
    scenarios = body["conflict_scenarios"]
    assert [
        (s["kind"], s["group"], s["winner"], tuple(s["suppressed_rule_codes"]))
        for s in scenarios
    ] == [
        ("group", "g", "r_a", ()),
        ("group", "g", "r_b", ("r_a",)),
    ]
    assert scenarios[0]["observed_count"] == 2
    assert scenarios[0]["observed_at_indexes"] == [0, 4]
    assert scenarios[1]["observed_count"] == 2
    assert scenarios[1]["observed_at_indexes"] == [1, 3]
    assert scenarios[1]["policy"] == "priority"
    # 场景携带其涉及规则的 rule 单元
    assert _unit_keys(scenarios[1]["coverage_units"]) == {
        ("rule", "r_a"),
        ("rule", "r_b"),
        ("group", "g", "r_b", ("r_a",)),
    }

    # 贪心：第 3 项一轮覆盖 4 个单元；剩余 g(r_a) 由 0/4 并列覆盖，取最小下标 0
    assert body["representative_indexes"] == [3, 0]

    # 成功项回显 fired/conflicts，且失败占位为空
    assert body["items"][1]["status"] == "success"
    assert body["items"][1]["fired_rule_codes"] == ["r_a", "r_b"]
    assert body["items"][1]["conflicts"] == [
        {
            "group": "g",
            "policy": "priority",
            "winner": "r_b",
            "suppressed": [
                {"rule_code": "r_a", "reason": "lower_priority", "priority": 1}
            ],
        }
    ]
    assert body["items"][1]["error"] is None


def test_coverage_field_overwrite_units(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "field-cov",
            "rules": [
                {"code": "f1", "condition": 'doc["k"] == "a"',
                 "action": '{"f": 1}'},
                {"code": "f2", "condition": 'doc["k"] == "a"',
                 "action": '{"f": 2}'},
            ],
        },
    ).json()["ruleset_id"]
    body = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{"k": "a"}, {"k": "b"}]},
    ).json()

    assert body["rule_coverage"] == [
        {"code": "f1", "hit_count": 1, "covered_by_indexes": [0]},
        {"code": "f2", "hit_count": 1, "covered_by_indexes": [0]},
    ]
    # 第二份文档无命中：两条规则都仍被 0 覆盖，故无未命中规则
    assert body["uncovered_rule_codes"] == []

    assert len(body["conflict_scenarios"]) == 1
    scenario = body["conflict_scenarios"][0]
    assert scenario["kind"] == "field"
    assert scenario["field"] == "f"
    assert scenario["previous_winner"] == "f1"
    assert scenario["winner"] == "f2"
    assert scenario["reason"] == "field_overwritten"
    assert scenario["suppressed_rule_codes"] == []
    assert scenario["observed_at_indexes"] == [0]
    assert _unit_keys(scenario["coverage_units"]) == {
        ("rule", "f1"),
        ("rule", "f2"),
        ("field", "f", "f1", "f2"),
    }
    assert body["representative_indexes"] == [0]
    # 第二项零覆盖单元，不入选
    assert body["items"][1]["coverage_units"] == []


def test_coverage_first_hit_suppressed_sorted(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "fh",
            "default_conflict_policy": "first_hit",
            "rules": [
                {"code": "h1", "conflict_group": "g",
                 "condition": 'doc["x"] >= 1', "action": '{"v": 1}'},
                {"code": "h2", "conflict_group": "g",
                 "condition": 'doc["x"] >= 1', "action": '{"v": 2}'},
                {"code": "h3", "conflict_group": "g",
                 "condition": 'doc["x"] >= 1', "action": '{"v": 3}'},
            ],
        },
    ).json()["ruleset_id"]
    body = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{"x": 1}]},
    ).json()
    scenario = body["conflict_scenarios"][0]
    assert scenario["kind"] == "group"
    assert scenario["policy"] == "first_hit"
    assert scenario["winner"] == "h1"
    # 被压制规则按编码排序，而非评估顺序
    assert scenario["suppressed_rule_codes"] == ["h2", "h3"]


def test_coverage_greedy_multi_round_and_zero_coverage(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "multi",
            "rules": [
                {"code": "p", "condition": 'doc["p"] == true',
                 "action": '{"p": 1}'},
                {"code": "q", "condition": 'doc["q"] == true',
                 "action": '{"q": 1}'},
            ],
        },
    ).json()["ruleset_id"]
    body = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={
            "version": 1,
            "documents": [
                {},  # 0 零覆盖
                {},  # 1 零覆盖
                {"p": True},  # 2 覆盖 p
                {"q": True},  # 3 覆盖 q
            ],
        },
    ).json()
    # 首轮 2/3 并列取最小下标 2，次轮再取 3；零覆盖项永不入选
    assert body["representative_indexes"] == [2, 3]
    assert body["uncovered_rule_codes"] == []

    # 全部成功文档都零覆盖时，代表列表为空
    body = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{}, {}]},
    ).json()
    assert body["representative_indexes"] == []
    assert body["uncovered_rule_codes"] == ["p", "q"]


def test_coverage_failed_item_isolated_and_excluded_from_selection(
    client, monkeypatch
):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "flaky",
            "rules": [
                {"code": "r_rare", "condition": 'doc["x"] >= 10',
                 "action": '{"v": 1}'},
            ],
        },
    ).json()["ruleset_id"]

    from app.engine import EvaluationFailed
    import app.main as main_module

    real_evaluate = main_module.evaluate

    def flaky_evaluate(compiled, document):
        if document.get("FAIL"):
            raise EvaluationFailed("NOT_CONVERGED", "模拟不收敛", {"k": 1})
        return real_evaluate(compiled, document)

    monkeypatch.setattr(main_module, "evaluate", flaky_evaluate)

    # 下标 1 本可命中 r_rare 但求值失败；下标 0 不命中
    resp = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{"x": 1}, {"x": 100, "FAIL": True}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["success_count"] == 1
    assert body["error_count"] == 1
    failed = body["items"][1]
    assert failed["status"] == "error"
    assert failed["fired_rule_codes"] == []
    assert failed["conflicts"] == []
    assert failed["coverage_units"] == []
    assert failed["error"] == {
        "code": "NOT_CONVERGED",
        "message": "模拟不收敛",
        "details": {"k": 1},
    }
    # 失败项不产生覆盖：r_rare 在唯一成功文档上未命中 → 未命中规则
    assert body["uncovered_rule_codes"] == ["r_rare"]
    assert body["rule_coverage"] == [
        {"code": "r_rare", "hit_count": 0, "covered_by_indexes": []}
    ]
    assert body["conflict_scenarios"] == []
    # 失败项不参与代表选择；成功项零覆盖 → 空列表（绝不返回下标 1）
    assert body["representative_indexes"] == []
    # 失败项不影响其他项
    assert body["items"][0]["status"] == "success"


def test_coverage_does_not_write_evaluations(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    before = client.get(f"/api/rulesets/{rid}/evaluations").json()
    resp = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{"x": 42}, {"x": 1}]},
    )
    assert resp.status_code == 200, resp.text
    after = client.get(f"/api/rulesets/{rid}/evaluations").json()
    assert before == after == []


def test_coverage_ruleset_not_found(client):
    resp = client.post(
        "/api/rulesets/nope/coverage",
        json={"version": 1, "documents": [{}]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_coverage_version_not_found(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 9, "documents": [{}]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"


def test_coverage_validates_document_count_and_version(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    # 缺少 version
    assert client.post(
        f"/api/rulesets/{rid}/coverage", json={"documents": [{}]}
    ).status_code == 422
    # version < 1
    assert client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 0, "documents": [{}]},
    ).status_code == 422
    # 空 documents
    assert client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": []},
    ).status_code == 422
    # 101 份超限
    assert client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{}] * 101},
    ).status_code == 422
    # 100 份与 1 份均可
    assert client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{}] * 100},
    ).status_code == 200
    assert client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{"x": 42}]},
    ).status_code == 200


def test_coverage_demo_ruleset(client, demo_id):
    body = client.post(
        f"/api/rulesets/{demo_id}/coverage",
        json={
            "version": 1,
            "documents": [
                # VIP 大额北方单：discount 组 priority，vip_discount 压制 large_order
                {"amount": 1500, "customer": {"vip": True}, "region": "north"},
                # 小额单：无任何规则命中
                {"amount": 80, "region": "north"},
            ],
        },
    ).json()

    assert body["total"] == 2 and body["error_count"] == 0
    assert [c["code"] for c in body["rule_coverage"]] == [
        "is_vip",
        "large_order",
        "vip_discount",
        "free_shipping",
        "coupon_save10",
        "coupon_save20",
        "manual_review",
    ]
    hit_counts = {c["code"]: c["hit_count"] for c in body["rule_coverage"]}
    assert hit_counts == {
        "is_vip": 1,
        "large_order": 1,
        "vip_discount": 1,
        "free_shipping": 1,
        "coupon_save10": 0,
        "coupon_save20": 0,
        "manual_review": 0,
    }
    assert body["uncovered_rule_codes"] == [
        "coupon_save10",
        "coupon_save20",
        "manual_review",
    ]

    assert len(body["conflict_scenarios"]) == 1
    scenario = body["conflict_scenarios"][0]
    assert scenario["kind"] == "group"
    assert scenario["group"] == "discount"
    assert scenario["winner"] == "vip_discount"
    assert scenario["suppressed_rule_codes"] == ["large_order"]
    assert scenario["observed_at_indexes"] == [0]

    # 第 0 项覆盖全部观察到的单元；第 1 项零覆盖
    assert body["representative_indexes"] == [0]
