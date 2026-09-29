from datetime import datetime, timedelta


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_demo_seeded_on_startup(client, demo_id):
    resp = client.get(f"/api/rulesets/{demo_id}")
    assert resp.status_code == 200
    assert resp.json()["current_version"] == 1


def test_create_and_evaluate_simple(client, simple_ruleset_payload):
    resp = client.post("/api/rulesets", json=simple_ruleset_payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    ruleset_id = body["ruleset_id"]
    assert body["version"] == 1
    assert body["evaluation_order"] == ["r1", "r2"]

    resp = client.post(
        f"/api/rulesets/{ruleset_id}/evaluate", json={"document": {"x": 42}}
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["fired_rule_codes"] == ["r1", "r2"]
    assert result["hit_rule_codes"] == ["r1", "r2"]
    assert result["decision"] == {"level": "high", "bonus": 1}
    assert result["iterations"] == 2
    assert result["id"]


def test_evaluate_non_matching_document(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    result = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 1}}
    ).json()
    assert result["fired_rule_codes"] == []
    assert result["decision"] == {}
    assert result["iterations"] == 1


def test_cyclic_ruleset_rejected_on_create(client):
    payload = {
        "name": "cyclic",
        "rules": [
            {"code": "a", "condition": "b.hit", "depends_on": ["b"]},
            {"code": "b", "condition": "a.hit == true", "depends_on": ["a"]},
        ],
    }
    resp = client.post("/api/rulesets", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "CYCLIC_DEPENDENCY"
    assert error["details"]["cycle"][0] == error["details"]["cycle"][-1]


def test_cyclic_ruleset_rejected_on_update(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    payload = dict(simple_ruleset_payload)
    payload["rules"] = [
        {"code": "a", "condition": "b.hit", "depends_on": ["b"]},
        {"code": "b", "condition": "a.hit", "depends_on": ["a"]},
    ]
    resp = client.put(f"/api/rulesets/{rid}", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CYCLIC_DEPENDENCY"
    # 更新失败，当前版本仍是 1
    meta = client.get(f"/api/rulesets/{rid}").json()
    assert meta["current_version"] == 1


def test_update_creates_new_version_and_keeps_old(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]

    # 旧版本下评估并保存结果
    old_eval = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    ).json()
    assert old_eval["version"] == 1

    # 修改：提高阈值并加新规则
    updated = {
        "name": "simple",
        "rules": [
            {"code": "r1", "condition": 'doc["x"] >= 1000',
             "action": '{"level": "high"}'},
            {"code": "r2", "condition": "r1.hit", "depends_on": ["r1"],
             "action": '{"bonus": 1}'},
        ],
    }
    resp = client.put(f"/api/rulesets/{rid}", json=updated)
    assert resp.status_code == 200
    assert resp.json()["version"] == 2

    meta = client.get(f"/api/rulesets/{rid}").json()
    assert meta["current_version"] == 2

    # 旧版本快照仍可查
    v1 = client.get(f"/api/rulesets/{rid}/versions/1").json()
    assert v1["rules"][0]["condition"] == 'doc["x"] >= 10'
    v2 = client.get(f"/api/rulesets/{rid}/versions/2").json()
    assert v2["rules"][0]["condition"] == 'doc["x"] >= 1000'

    # 版本列表
    versions = client.get(f"/api/rulesets/{rid}/versions").json()
    assert [v["version"] for v in versions] == [1, 2]

    # 同一文档在最新版上不再命中，但可用旧版重算
    new_eval = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    ).json()
    assert new_eval["version"] == 2
    assert new_eval["fired_rule_codes"] == []

    old_again = client.post(
        f"/api/rulesets/{rid}/evaluate?version=1",
        json={"document": {"x": 100}},
    ).json()
    assert old_again["version"] == 1
    assert old_again["fired_rule_codes"] == ["r1", "r2"]


def test_old_evaluations_remain_queryable(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    e1 = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    ).json()
    client.put(
        f"/api/rulesets/{rid}",
        json={**simple_ruleset_payload, "name": "simple2"},
    )
    e2 = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    ).json()
    assert e2["version"] == 2

    listing = client.get(f"/api/rulesets/{rid}/evaluations").json()
    assert {e["id"] for e in listing} == {e1["id"], e2["id"]}
    assert {e["version"] for e in listing} == {1, 2}

    got = client.get(f"/api/evaluations/{e1['id']}").json()
    assert got["version"] == 1
    assert got["document"] == {"x": 100}


def test_recompute_with_latest_version(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    )
    # 规则收紧，x=100 在新版不命中
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "simple",
            "rules": [
                {"code": "r1", "condition": 'doc["x"] >= 1000'},
            ],
        },
    )
    # 不带 document，重放最近一次文档
    resp = client.post(f"/api/rulesets/{rid}/recompute", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 2
    assert body["document"] == {"x": 100}
    assert body["fired_rule_codes"] == []
    # 旧结果仍保留：共两条历史
    assert len(client.get(f"/api/rulesets/{rid}/evaluations").json()) == 2


def test_recompute_without_history_requires_document(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(f"/api/rulesets/{rid}/recompute", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "NO_DOCUMENT_TO_RECOMPUTE"


def test_evaluate_persist_flag(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/evaluate?persist=false",
        json={"document": {"x": 1}},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == ""
    assert client.get(f"/api/rulesets/{rid}/evaluations").json() == []


def test_conflict_explanations_in_response(client, demo_id):
    # VIP + 大额：discount 组中 vip_discount(priority20) 压制 large_order(10)
    resp = client.post(
        f"/api/rulesets/{demo_id}/evaluate",
        json={"document": {"amount": 1500, "customer": {"vip": True}, "region": "north"}},
    )
    result = resp.json()
    assert "vip_discount" in result["fired_rule_codes"]
    assert "large_order" in result["fired_rule_codes"]
    assert "large_order" not in result["hit_rule_codes"]
    assert "vip_discount" in result["hit_rule_codes"]
    discount_conflict = next(c for c in result["conflicts"] if c["group"] == "discount")
    assert discount_conflict["policy"] == "priority"
    assert discount_conflict["winner"] == "vip_discount"
    suppressed_codes = [s["rule_code"] for s in discount_conflict["suppressed"]]
    assert suppressed_codes == ["large_order"]
    assert result["decision"]["discount"] == 0.15
    assert result["decision"]["free_shipping"] is True


def test_first_hit_coupon_conflict(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/evaluate",
        json={"document": {"amount": 10, "coupons": ["SAVE10", "SAVE20"]}},
    )
    result = resp.json()
    coupon_conflict = next(c for c in result["conflicts"] if c["group"] == "coupon")
    assert coupon_conflict["policy"] == "first_hit"
    assert coupon_conflict["winner"] == "coupon_save10"
    assert result["decision"]["coupon"] == "SAVE10"


def test_manual_review_for_non_vip_huge_order(client, demo_id):
    # 3000 但非 VIP：large_order 命中、vip_discount 不命中 -> 人工审核命中
    resp = client.post(
        f"/api/rulesets/{demo_id}/evaluate",
        json={"document": {"amount": 3000, "customer": {"vip": False}}},
    )
    result = resp.json()
    assert "manual_review" in result["hit_rule_codes"]
    assert result["decision"]["manual_review"] is True


def test_no_review_when_vip(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/evaluate",
        json={"document": {"amount": 5000, "customer": {"vip": True}}},
    )
    result = resp.json()
    assert "manual_review" not in result["hit_rule_codes"]


def test_404_paths(client):
    assert client.get("/api/rulesets/nope").status_code == 404
    assert client.get("/api/rulesets/nope/versions").status_code == 404
    assert client.get("/api/rulesets/nope/versions/1").status_code == 404
    assert client.post(
        "/api/rulesets/nope/evaluate", json={"document": {}}
    ).status_code == 404
    assert client.get("/api/evaluations/nope").status_code == 404


def test_custom_id_and_duplicate(client):
    payload = {"id": "my-set", "name": "n", "rules": [{"code": "a", "condition": "true"}]}
    assert client.post("/api/rulesets", json=payload).status_code == 201
    dup = client.post("/api/rulesets", json=payload)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "RULESET_ID_EXISTS"


def test_undeclared_reference_rejected(client):
    payload = {
        "name": "n",
        "rules": [
            {"code": "a"},
            {"code": "b", "condition": "a.hit"},  # 缺少 depends_on
        ],
    }
    resp = client.post("/api/rulesets", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNDECLARED_REFERENCE"


# ---------- 批量评估 ----------


def test_evaluate_batch_basic(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    docs = [{"x": 42}, {"x": 1}, {"x": 100}]
    resp = client.post(f"/api/rulesets/{rid}/evaluate-batch", json={"documents": docs})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ruleset_id"] == rid
    assert body["version"] == 1
    assert body["total"] == 3
    assert body["success_count"] == 3
    assert body["error_count"] == 0

    # 结果严格按输入 index 排列
    assert [item["index"] for item in body["results"]] == [0, 1, 2]
    assert all(item["status"] == "success" for item in body["results"])
    assert all(item["error"] is None for item in body["results"])
    assert body["results"][0]["result"]["decision"] == {"level": "high", "bonus": 1}
    assert body["results"][1]["result"]["decision"] == {}
    assert body["results"][2]["result"]["decision"] == {"level": "high", "bonus": 1}

    # 默认持久化：三项都应可在历史中查到
    listing = client.get(f"/api/rulesets/{rid}/evaluations").json()
    assert len(listing) == 3
    saved_ids = {item["result"]["id"] for item in body["results"]}
    assert all(saved_ids)  # id 非空
    assert {e["id"] for e in listing} == saved_ids


def test_evaluate_batch_empty_documents(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(f"/api/rulesets/{rid}/evaluate-batch", json={"documents": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["success_count"] == 0
    assert body["error_count"] == 0
    assert body["results"] == []


def test_evaluate_batch_rejects_more_than_100(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/evaluate-batch",
        json={"documents": [{"x": i} for i in range(101)]},
    )
    assert resp.status_code == 422


def test_evaluate_batch_accepts_exactly_100(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/evaluate-batch",
        json={"documents": [{"x": i} for i in range(100)]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 100
    assert body["success_count"] == 100
    assert [item["index"] for item in body["results"]] == list(range(100))


def test_evaluate_batch_item_error_does_not_abort(client, simple_ruleset_payload, monkeypatch):
    from app.engine import EvaluationFailed
    import app.main as main_module

    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]

    real_evaluate = main_module.evaluate

    def flaky_evaluate(compiled, document):
        if document.get("boom"):
            raise EvaluationFailed(
                "NOT_CONVERGED", "模拟不收敛", {"max_iterations": 100}
            )
        return real_evaluate(compiled, document)

    monkeypatch.setattr(main_module, "evaluate", flaky_evaluate)

    resp = client.post(
        f"/api/rulesets/{rid}/evaluate-batch",
        json={"documents": [{"x": 42}, {"boom": True}, {"x": 100}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success_count"] == 2
    assert body["error_count"] == 1

    ok0, bad1, ok2 = body["results"]
    assert ok0["index"] == 0 and ok0["status"] == "success"
    assert ok0["result"]["decision"] == {"level": "high", "bonus": 1}
    assert bad1["index"] == 1 and bad1["status"] == "error"
    assert bad1["result"] is None
    assert bad1["error"]["code"] == "NOT_CONVERGED"
    assert bad1["error"]["details"] == {"max_iterations": 100}
    assert ok2["index"] == 2 and ok2["status"] == "success"
    assert ok2["result"]["decision"] == {"level": "high", "bonus": 1}

    # 失败项不落库，成功项照常落库
    listing = client.get(f"/api/rulesets/{rid}/evaluations").json()
    assert {e["id"] for e in listing} == {ok0["result"]["id"], ok2["result"]["id"]}


def test_evaluate_batch_serialization_error_isolated(client, monkeypatch):
    # 动作返回 set：引擎可求值，但落库 JSON 序列化失败，
    # 该项记为 error 而不影响其他项
    payload = {
        "name": "set-action",
        "rules": [{"code": "s", "condition": "true", "action": "{1, 2}"}],
    }
    rid = client.post("/api/rulesets", json=payload).json()["ruleset_id"]

    import app.crud as crud_module

    def broken_save(*args, **kwargs):
        raise TypeError("Object of type set is not JSON serializable")

    monkeypatch.setattr(crud_module, "save_evaluation", broken_save)

    resp = client.post(
        f"/api/rulesets/{rid}/evaluate-batch",
        json={"documents": [{"x": 1}, {"x": 2}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success_count"] == 0
    assert body["error_count"] == 2
    for item in body["results"]:
        assert item["status"] == "error"
        assert item["error"]["code"] == "EVALUATION_ERROR"


def test_evaluate_batch_uses_single_pinned_version(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    # 收紧为 v2：x=100 不再命中
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "simple",
            "rules": [{"code": "r1", "condition": 'doc["x"] >= 1000'}],
        },
    )

    resp = client.post(
        f"/api/rulesets/{rid}/evaluate-batch",
        json={"documents": [{"x": 100}, {"x": 5000}], "version": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    # 整批固定使用 v1
    assert body["version"] == 1
    assert all(item["result"]["version"] == 1 for item in body["results"])
    # v1 阈值 10：x=100 命中 r1+r2
    assert body["results"][0]["result"]["fired_rule_codes"] == ["r1", "r2"]
    assert body["results"][1]["result"]["fired_rule_codes"] == ["r1", "r2"]

    # 缺省 version 时整批使用最新版 v2
    resp = client.post(
        f"/api/rulesets/{rid}/evaluate-batch",
        json={"documents": [{"x": 100}, {"x": 5000}]},
    )
    body = resp.json()
    assert body["version"] == 2
    assert body["results"][0]["result"]["fired_rule_codes"] == []
    assert body["results"][1]["result"]["fired_rule_codes"] == ["r1"]


def test_evaluate_batch_missing_versions_and_ruleset(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/evaluate-batch",
        json={"documents": [{"x": 1}], "version": 99},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"

    resp = client.post(
        "/api/rulesets/nope/evaluate-batch", json={"documents": [{"x": 1}]}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_evaluate_batch_persist_false(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/evaluate-batch?persist=false",
        json={"documents": [{"x": 42}, {"x": 1}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success_count"] == 2
    assert all(item["result"]["id"] == "" for item in body["results"])
    assert client.get(f"/api/rulesets/{rid}/evaluations").json() == []


# ---------- 评估统计 ----------


def test_evaluation_stats_basic(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    # x=42 / x=100 命中 r1+r2，x=1 均不命中
    for x in (42, 1, 100):
        resp = client.post(f"/api/rulesets/{rid}/evaluate", json={"document": {"x": x}})
        assert resp.status_code == 200

    resp = client.get(f"/api/rulesets/{rid}/evaluation-stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ruleset_id"] == rid
    assert body["version"] is None
    assert body["since"] is None
    assert body["until"] is None
    assert body["total_evaluations"] == 3
    # 按 code 稳定排序；r1/r2 在 3 次决策中各命中 2 次
    assert body["rule_stats"] == [
        {"code": "r1", "hit_count": 2, "hit_rate": 0.6667},
        {"code": "r2", "hit_count": 2, "hit_rate": 0.6667},
    ]
    # 决策字段按名称排序，统计出现的决策条数
    assert body["decision_field_counts"] == [
        {"field": "bonus", "count": 2},
        {"field": "level", "count": 2},
    ]


def test_evaluation_stats_version_filter(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    client.post(f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}})
    # 收紧为 v2：只剩 r1 且阈值 1000，x=100 不再命中
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "simple",
            "rules": [{"code": "r1", "condition": 'doc["x"] >= 1000'}],
        },
    )
    client.post(f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}})

    body = client.get(f"/api/rulesets/{rid}/evaluation-stats").json()
    assert body["total_evaluations"] == 2

    v1 = client.get(f"/api/rulesets/{rid}/evaluation-stats?version=1").json()
    assert v1["version"] == 1
    assert v1["total_evaluations"] == 1
    assert v1["rule_stats"] == [
        {"code": "r1", "hit_count": 1, "hit_rate": 1.0},
        {"code": "r2", "hit_count": 1, "hit_rate": 1.0},
    ]
    assert v1["decision_field_counts"] == [
        {"field": "bonus", "count": 1},
        {"field": "level", "count": 1},
    ]

    v2 = client.get(f"/api/rulesets/{rid}/evaluation-stats?version=2").json()
    assert v2["version"] == 2
    assert v2["total_evaluations"] == 1
    # 未命中的规则也出现在 traces 中，hit_count 为 0
    assert v2["rule_stats"] == [{"code": "r1", "hit_count": 0, "hit_rate": 0.0}]
    assert v2["decision_field_counts"] == []

    # 没有任何记录的版本：零值结构而非 404
    v9 = client.get(f"/api/rulesets/{rid}/evaluation-stats?version=9").json()
    assert v9["total_evaluations"] == 0
    assert v9["rule_stats"] == []
    assert v9["decision_field_counts"] == []


def test_evaluation_stats_since_until(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    e1 = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    ).json()
    e2 = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 1}}
    ).json()
    t1 = datetime.fromisoformat(e1["created_at"])
    t2 = datetime.fromisoformat(e2["created_at"])
    assert t1 <= t2

    # 含边界：闭区间 [t1, t2] 覆盖两条记录，并回显归一化后的筛选条件
    body = client.get(
        f"/api/rulesets/{rid}/evaluation-stats",
        params={"since": e1["created_at"], "until": e2["created_at"]},
    ).json()
    assert body["total_evaluations"] == 2
    assert body["since"] == t1.isoformat(timespec="seconds")
    assert body["until"] == t2.isoformat(timespec="seconds")

    # 边界之外：一条都匹配不到
    before = (t1 - timedelta(seconds=1)).isoformat()
    after = (t2 + timedelta(seconds=1)).isoformat()
    body = client.get(
        f"/api/rulesets/{rid}/evaluation-stats", params={"until": before}
    ).json()
    assert body["total_evaluations"] == 0
    body = client.get(
        f"/api/rulesets/{rid}/evaluation-stats", params={"since": after}
    ).json()
    assert body["total_evaluations"] == 0

    # 带时区偏移与 Z 结尾都能解析，并归一化为 UTC 秒级字符串
    body = client.get(
        f"/api/rulesets/{rid}/evaluation-stats",
        params={"since": "2000-01-01T08:00:00+08:00", "until": "2999-12-31T23:59:59Z"},
    ).json()
    assert body["total_evaluations"] == 2
    assert body["since"] == "2000-01-01T00:00:00+00:00"
    assert body["until"] == "2999-12-31T23:59:59+00:00"


def test_evaluation_stats_invalid_time_params(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.get(f"/api/rulesets/{rid}/evaluation-stats?since=not-a-time")
    assert resp.status_code == 422
    resp = client.get(f"/api/rulesets/{rid}/evaluation-stats?until=2026-13-40T99:99")
    assert resp.status_code == 422


def test_evaluation_stats_ruleset_not_found(client):
    resp = client.get("/api/rulesets/nope/evaluation-stats")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_evaluation_stats_empty_and_unpersisted_ignored(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    # persist=false 的试算不计入统计
    client.post(
        f"/api/rulesets/{rid}/evaluate?persist=false", json={"document": {"x": 100}}
    )
    resp = client.get(f"/api/rulesets/{rid}/evaluation-stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_evaluations"] == 0
    assert body["rule_stats"] == []
    assert body["decision_field_counts"] == []


def test_evaluation_stats_uses_traces_including_suppressed(client, demo_id):
    # VIP + 大额：large_order 命中但被 vip_discount 压制；traces 中 hit=true 仍计入
    client.post(
        f"/api/rulesets/{demo_id}/evaluate",
        json={"document": {"amount": 1500, "customer": {"vip": True}, "region": "north"}},
    )
    body = client.get(f"/api/rulesets/{demo_id}/evaluation-stats").json()
    assert body["total_evaluations"] == 1
    stats = {s["code"]: s for s in body["rule_stats"]}
    assert stats["large_order"] == {"code": "large_order", "hit_count": 1, "hit_rate": 1.0}
    assert stats["vip_discount"]["hit_count"] == 1
    assert stats["manual_review"]["hit_count"] == 0
    # 全部规则按 code 排序出现
    assert [s["code"] for s in body["rule_stats"]] == sorted(stats)
    # 决策字段只统计最终 decision 的顶层字段（被压制的 large_order 字段不计）
    assert body["decision_field_counts"] == [
        {"field": "discount", "count": 1},
        {"field": "free_shipping", "count": 1},
        {"field": "reason", "count": 1},
        {"field": "vip", "count": 1},
    ]
