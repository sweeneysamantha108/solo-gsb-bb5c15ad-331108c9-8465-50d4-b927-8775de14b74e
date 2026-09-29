"""POST /api/rulesets/{id}/compare 接口测试：跨版本预演、不落库、错误隔离。"""


def _create_two_versions(client, payload_v1, payload_v2, ruleset_id=None):
    body = {"id": ruleset_id, **payload_v1} if ruleset_id else payload_v1
    rid = client.post("/api/rulesets", json=body).json()["ruleset_id"]
    client.put(f"/api/rulesets/{rid}", json=payload_v2)
    return rid


def test_compare_cross_version_added_removed_changed(client):
    rid = _create_two_versions(
        client,
        payload_v1={
            "name": "simple",
            "rules": [
                {"code": "r1", "condition": 'doc["x"] >= 10',
                 "action": '{"level": "high"}'},
                {"code": "r2", "condition": "r1.hit", "depends_on": ["r1"],
                 "action": '{"bonus": 1}'},
            ],
        },
        payload_v2={
            "name": "simple",
            "rules": [
                {"code": "r1", "condition": 'doc["x"] >= 1000',
                 "action": '{"level": "high"}'},
                {"code": "r3", "condition": "true",
                 "action": '{"extra": {"n": 7}}'},
            ],
        },
    )

    # x=42：v1 命中 r1+r2，v2 只有恒真的 r3 命中
    resp = client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 1, "target_version": 2, "document": {"x": 42}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["ruleset_id"] == rid
    assert body["base_version"] == 1 and body["target_version"] == 2

    # 两侧分别按各自版本求值，完整结果回显
    base = body["base"]
    assert base["version"] == 1 and base["status"] == "success"
    assert base["error"] is None
    assert base["result"]["version"] == 1
    assert base["result"]["id"] == ""  # 不落库
    assert base["result"]["fired_rule_codes"] == ["r1", "r2"]
    assert base["result"]["decision"] == {"level": "high", "bonus": 1}

    target = body["target"]
    assert target["version"] == 2 and target["status"] == "success"
    assert target["result"]["fired_rule_codes"] == ["r3"]
    assert target["result"]["decision"] == {"extra": {"n": 7}}

    # 命中规则摘要
    assert body["summary"]["fired_rules"] == {
        "base": ["r1", "r2"], "target": ["r3"],
        "added": ["r3"], "removed": ["r1", "r2"],
    }
    assert body["summary"]["hit_rules"] == {
        "base": ["r1", "r2"], "target": ["r3"],
        "added": ["r3"], "removed": ["r1", "r2"],
    }

    # 决策差异按 JSON 路径归并
    diff = body["decision_diff"]
    assert diff["added"] == [
        {"path": "/extra/n", "old": None, "new": 7},
    ]
    assert diff["removed"] == [
        {"path": "/bonus", "old": 1, "new": None},
        {"path": "/level", "old": "high", "new": None},
    ]
    assert diff["changed"] == []


def test_compare_cross_version_conflict_winner_changes(client):
    rid = _create_two_versions(
        client,
        payload_v1={
            "name": "discounts",
            "default_conflict_policy": "priority",
            "rules": [
                {"code": "r_low", "priority": 10, "conflict_group": "discount",
                 "condition": 'doc["amount"] >= 1000',
                 "action": '{"discount": 0.1}'},
                {"code": "r_hi", "priority": 20, "conflict_group": "discount",
                 "condition": 'doc["amount"] >= 1000',
                 "action": '{"discount": 0.2}'},
            ],
        },
        # v2：低优规则反超为高优先级
        payload_v2={
            "name": "discounts",
            "default_conflict_policy": "priority",
            "rules": [
                {"code": "r_low", "priority": 30, "conflict_group": "discount",
                 "condition": 'doc["amount"] >= 1000',
                 "action": '{"discount": 0.1}'},
                {"code": "r_hi", "priority": 20, "conflict_group": "discount",
                 "condition": 'doc["amount"] >= 1000',
                 "action": '{"discount": 0.2}'},
            ],
        },
    )

    body = client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 1, "target_version": 2,
              "document": {"amount": 2000}},
    ).json()

    # 两侧 fired 相同（都命中），冲突胜出方翻转
    assert body["summary"]["fired_rules"]["added"] == []
    assert body["summary"]["fired_rules"]["removed"] == []
    changed = body["summary"]["conflicts"]["changed"]
    assert len(changed) == 1
    assert changed[0]["group"] == "discount"
    assert changed[0]["field"] is None
    assert changed[0]["old"]["winner"] == "r_hi"
    assert changed[0]["new"]["winner"] == "r_low"
    assert body["decision_diff"]["changed"] == [
        {"path": "/discount", "old": 0.2, "new": 0.1}
    ]


def test_compare_empty_diff_same_version(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    body = client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 1, "target_version": 1, "document": {"x": 42}},
    ).json()
    assert body["base"]["status"] == "success"
    assert body["target"]["status"] == "success"
    assert body["decision_diff"] == {"added": [], "removed": [], "changed": []}
    summary = body["summary"]
    assert summary["fired_rules"]["added"] == []
    assert summary["fired_rules"]["removed"] == []
    assert summary["hit_rules"]["added"] == []
    assert summary["conflicts"]["added"] == []
    assert summary["conflicts"]["removed"] == []
    assert summary["conflicts"]["changed"] == []


def test_compare_empty_diff_equivalent_cross_version(client, simple_ruleset_payload):
    # v2 仅改了描述、求值语义不变：跨版本结果应无差异
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "simple-renamed",
            "description": "same semantics",
            "rules": [
                {"code": "r1", "condition": 'doc["x"] >= 10',
                 "action": '{"level": "high"}'},
                {"code": "r2", "condition": "r1.hit", "depends_on": ["r1"],
                 "action": '{"bonus": 1}'},
            ],
        },
    )
    body = client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 1, "target_version": 2, "document": {"x": 1}},
    ).json()
    assert body["decision_diff"] == {"added": [], "removed": [], "changed": []}
    assert body["summary"]["fired_rules"] == {
        "base": [], "target": [], "added": [], "removed": []
    }


def test_compare_does_not_write_evaluations(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    client.put(
        f"/api/rulesets/{rid}",
        json={"name": "simple", "rules": [{"code": "r1", "condition": "true"}]},
    )
    before = client.get(f"/api/rulesets/{rid}/evaluations").json()
    resp = client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 1, "target_version": 2, "document": {"x": 42}},
    )
    assert resp.status_code == 200
    after = client.get(f"/api/rulesets/{rid}/evaluations").json()
    assert before == after == []
    # 两侧记录 id 均为空
    assert resp.json()["base"]["result"]["id"] == ""
    assert resp.json()["target"]["result"]["id"] == ""


def test_compare_evaluation_failure_isolated_per_side(client, monkeypatch):
    # v1 正常；v2 通过名字识别，令其求值抛 EvaluationFailed（模拟不收敛）
    rid = _create_two_versions(
        client,
        payload_v1={
            "name": "stable",
            "rules": [{"code": "r1", "condition": "true",
                       "action": '{"ok": 1}'}],
        },
        payload_v2={
            "name": "broken-ruleset",
            "rules": [{"code": "r1", "condition": "true",
                       "action": '{"ok": 2}'}],
        },
    )

    from app.engine import EvaluationFailed
    import app.main as main_module

    real_evaluate = main_module.evaluate

    def flaky_evaluate(compiled, document):
        if compiled.name == "broken-ruleset":
            raise EvaluationFailed(
                "NOT_CONVERGED", "模拟不收敛", {"max_iterations": 100}
            )
        return real_evaluate(compiled, document)

    monkeypatch.setattr(main_module, "evaluate", flaky_evaluate)

    resp = client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 1, "target_version": 2, "document": {}},
    )
    # 单侧失败不把整请求变成错误码，仍返回 200 并隔离在该侧
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["base"]["status"] == "success"
    assert body["base"]["result"]["decision"] == {"ok": 1}
    assert body["target"]["status"] == "error"
    assert body["target"]["result"] is None
    assert body["target"]["error"] == {
        "code": "NOT_CONVERGED",
        "message": "模拟不收敛",
        "details": {"max_iterations": 100},
    }
    # 任一侧失败时不产出摘要与差异
    assert body["summary"] is None
    assert body["decision_diff"] is None
    # 失败侧同样没有落库
    assert client.get(f"/api/rulesets/{rid}/evaluations").json() == []


def test_compare_base_failure_keeps_target_result(client, monkeypatch):
    rid = _create_two_versions(
        client,
        payload_v1={
            "name": "broken-ruleset",
            "rules": [{"code": "r1", "condition": "true",
                       "action": '{"ok": 1}'}],
        },
        payload_v2={
            "name": "stable",
            "rules": [{"code": "r1", "condition": "true",
                       "action": '{"ok": 2}'}],
        },
    )

    from app.engine import EvaluationFailed
    import app.main as main_module

    real_evaluate = main_module.evaluate

    def flaky_evaluate(compiled, document):
        if compiled.name == "broken-ruleset":
            raise EvaluationFailed("NOT_CONVERGED", "基准版失败", {})
        return real_evaluate(compiled, document)

    monkeypatch.setattr(main_module, "evaluate", flaky_evaluate)

    body = client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 1, "target_version": 2, "document": {}},
    ).json()
    assert body["base"]["status"] == "error"
    assert body["base"]["error"]["code"] == "NOT_CONVERGED"
    assert body["target"]["status"] == "success"
    assert body["target"]["result"]["decision"] == {"ok": 2}
    assert body["summary"] is None and body["decision_diff"] is None


def test_compare_ruleset_not_found(client):
    resp = client.post(
        "/api/rulesets/nope/compare",
        json={"base_version": 1, "target_version": 2, "document": {}},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_compare_version_not_found(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 1, "target_version": 9, "document": {}},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"

    resp = client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 9, "target_version": 1, "document": {}},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"


def test_compare_validates_request_body(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    # 缺少必填版本
    assert client.post(
        f"/api/rulesets/{rid}/compare",
        json={"target_version": 1, "document": {}},
    ).status_code == 422
    # version < 1
    assert client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 0, "target_version": 1, "document": {}},
    ).status_code == 422
    # document 缺省允许（默认 {}）
    resp = client.post(
        f"/api/rulesets/{rid}/compare",
        json={"base_version": 1, "target_version": 1},
    )
    assert resp.status_code == 200, resp.text


def test_compare_demo_ruleset_versions(client, demo_id):
    # 演示规则集只有 v1：v1 自比应全空
    body = client.post(
        f"/api/rulesets/{demo_id}/compare",
        json={
            "base_version": 1,
            "target_version": 1,
            "document": {"amount": 1500, "customer": {"vip": True}, "region": "north"},
        },
    ).json()
    assert body["ruleset_id"] == demo_id
    assert body["decision_diff"] == {"added": [], "removed": [], "changed": []}
    # 两侧完整结果与 evaluate 语义一致（含冲突解释，但无 id）
    result = body["base"]["result"]
    assert "vip_discount" in result["hit_rule_codes"]
    assert "large_order" not in result["hit_rule_codes"]
    assert result["id"] == ""
