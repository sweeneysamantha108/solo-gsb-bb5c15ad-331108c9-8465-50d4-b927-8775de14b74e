"""决策人工更正（修订）接口测试。"""

import threading
import time


def _create_ruleset_and_evaluate(client, document=None):
    payload = {
        "name": "corr",
        "rules": [
            {"code": "r1", "condition": 'doc["x"] >= 10',
             "action": '{"level": "high"}'},
            {"code": "r2", "condition": "r1.hit", "depends_on": ["r1"],
             "action": '{"bonus": 1}'},
        ],
    }
    rid = client.post("/api/rulesets", json=payload).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": document or {"x": 42}}
    )
    assert resp.status_code == 200, resp.text
    return rid, resp.json()


def _correction_url(eval_id, suffix=""):
    return f"/api/evaluations/{eval_id}/corrections{suffix}"


# ---------- 首次更正 ----------


def test_first_correction_creates_revision_1(client):
    rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]
    assert evaluation["decision"] == {"level": "high", "bonus": 1}

    # 无更正时：当前有效决策即原计算结果，修订号 0
    eff = client.get(f"/api/evaluations/{eval_id}/effective-decision")
    assert eff.status_code == 200
    assert eff.json() == {
        "evaluation_id": eval_id,
        "current_revision": 0,
        "decision": {"level": "high", "bonus": 1},
        "corrected": False,
        "latest_correction_id": None,
    }

    resp = client.post(
        _correction_url(eval_id),
        json={
            "base_revision": 0,
            "request_no": "review-001",
            "decision": {"level": "high", "bonus": 1, "manual": True},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"]
    assert body["evaluation_id"] == eval_id
    assert body["revision"] == 1
    assert body["base_revision"] == 0
    assert body["request_no"] == "review-001"
    assert body["decision"] == {"level": "high", "bonus": 1, "manual": True}
    assert body["created_at"]

    # 当前有效决策已替换为人工更正
    eff = client.get(f"/api/evaluations/{eval_id}/effective-decision").json()
    assert eff["current_revision"] == 1
    assert eff["corrected"] is True
    assert eff["latest_correction_id"] == body["id"]
    assert eff["decision"] == {"level": "high", "bonus": 1, "manual": True}

    # 更正列表按修订号升序
    listing = client.get(_correction_url(eval_id)).json()
    assert listing["current_revision"] == 1
    assert [c["revision"] for c in listing["items"]] == [1]
    assert listing["items"][0]["id"] == body["id"]

    # 原始决策记录保持不变（含所用规则版本与原始 decision）
    original = client.get(f"/api/evaluations/{eval_id}").json()
    assert original["decision"] == {"level": "high", "bonus": 1}
    assert original["version"] == 1
    assert original["id"] == eval_id

    # 评估统计只看原始落库结果，不受更正影响
    stats = client.get(f"/api/rulesets/{rid}/evaluation-stats").json()
    assert stats["total_evaluations"] == 1
    assert stats["decision_field_counts"] == [
        {"field": "bonus", "count": 1},
        {"field": "level", "count": 1},
    ]


def test_sequential_corrections_increment_revision(client):
    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]

    c1 = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "r-a",
              "decision": {"v": 1}},
    )
    assert c1.status_code == 201
    assert c1.json()["revision"] == 1

    c2 = client.post(
        _correction_url(eval_id),
        json={"base_revision": 1, "request_no": "r-b",
              "decision": {"v": 2}},
    )
    assert c2.status_code == 201
    assert c2.json()["revision"] == 2

    c3 = client.post(
        _correction_url(eval_id),
        json={"base_revision": 2, "request_no": "r-c",
              "decision": {"v": 3}},
    )
    assert c3.status_code == 201
    assert c3.json()["revision"] == 3

    eff = client.get(f"/api/evaluations/{eval_id}/effective-decision").json()
    assert eff["current_revision"] == 3
    assert eff["decision"] == {"v": 3}
    assert eff["latest_correction_id"] == c3.json()["id"]

    listing = client.get(_correction_url(eval_id)).json()
    assert [c["revision"] for c in listing["items"]] == [1, 2, 3]
    assert [c["request_no"] for c in listing["items"]] == ["r-a", "r-b", "r-c"]


# ---------- 幂等重试 ----------


def test_same_request_replay_returns_first_correction(client):
    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]
    payload = {"base_revision": 0, "request_no": "idem-1",
               "decision": {"v": 1}}

    first = client.post(_correction_url(eval_id), json=payload)
    assert first.status_code == 201

    # 完全相同重试 → 200 + 首次记录，不重复写入
    replay = client.post(_correction_url(eval_id), json=payload)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["revision"] == 1

    listing = client.get(_correction_url(eval_id)).json()
    assert len(listing["items"]) == 1


def test_replay_still_returns_first_after_later_corrections(client):
    """首次更正之后已有后续更正，同请求号同基准同内容重试仍返回首次记录。"""
    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]

    first = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "idem-1",
              "decision": {"v": 1}},
    ).json()

    # 后续另一笔更正把当前修订号推进到 2（idem-1 的基准 0 已过期）
    second = client.post(
        _correction_url(eval_id),
        json={"base_revision": 1, "request_no": "idem-2",
              "decision": {"v": 2}},
    )
    assert second.status_code == 201

    replay = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "idem-1",
              "decision": {"v": 1}},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == first["id"]
    assert replay.json()["revision"] == 1

    listing = client.get(_correction_url(eval_id)).json()
    assert [c["revision"] for c in listing["items"]] == [1, 2]


def test_replay_canonicalizes_decision_object_keys(client):
    """替代 decision 对象键序不同但内容相同，视为同一请求回放。"""
    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]

    first = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "canon",
              "decision": {"a": 1, "b": {"y": 2, "x": 1}}},
    )
    assert first.status_code == 201

    replay = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "canon",
              "decision": {"b": {"x": 1, "y": 2}, "a": 1}},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


def test_request_no_surrounding_whitespace_trimmed(client):
    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]

    first = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "  req-1  ",
              "decision": {"v": 1}},
    )
    assert first.status_code == 201
    assert first.json()["request_no"] == "req-1"

    replay = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "req-1",
              "decision": {"v": 1}},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


# ---------- 冲突 ----------


def test_stale_base_revision_conflict(client):
    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]

    client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "a", "decision": {"v": 1}},
    )

    # 新请求号但基准过期（当前已为 1）→ 409 修订冲突，不写入
    resp = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "b", "decision": {"v": 2}},
    )
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "CORRECTION_REVISION_CONFLICT"
    assert error["details"]["base_revision"] == 0
    assert error["details"]["current_revision"] == 1

    listing = client.get(_correction_url(eval_id)).json()
    assert [c["revision"] for c in listing["items"]] == [1]


def test_same_request_different_base_revision_conflict(client):
    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]

    client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "dup", "decision": {"v": 1}},
    )
    client.post(
        _correction_url(eval_id),
        json={"base_revision": 1, "request_no": "other", "decision": {"v": 2}},
    )

    # 同请求号但基准修订号不同 → 409 请求冲突（field=base_revision）
    resp = client.post(
        _correction_url(eval_id),
        json={"base_revision": 1, "request_no": "dup", "decision": {"v": 1}},
    )
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "CORRECTION_REQUEST_CONFLICT"
    assert error["details"]["field"] == "base_revision"
    assert error["details"]["original"] == 0
    assert error["details"]["conflicting"] == 1

    assert len(client.get(_correction_url(eval_id)).json()["items"]) == 2


def test_same_request_different_decision_conflict(client):
    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]

    client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "dup", "decision": {"v": 1}},
    )

    # 同请求号同基准但替代内容不同 → 409 请求冲突（field=decision）
    resp = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "dup", "decision": {"v": 2}},
    )
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "CORRECTION_REQUEST_CONFLICT"
    assert error["details"]["field"] == "decision"

    # 数组顺序属于语义：内容不同也冲突
    resp2 = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "dup",
              "decision": {"v": 1, "extra": [2, 1]}},
    )
    assert resp2.status_code == 409
    assert resp2.json()["error"]["details"]["field"] == "decision"

    assert len(client.get(_correction_url(eval_id)).json()["items"]) == 1


# ---------- 参数校验 / 不存在 ----------


def test_correction_evaluation_not_found(client):
    resp = client.post(
        _correction_url("missing-eval"),
        json={"base_revision": 0, "request_no": "x", "decision": {"v": 1}},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"

    assert (
        client.get("/api/evaluations/missing-eval/effective-decision").status_code
        == 404
    )
    assert client.get(_correction_url("missing-eval")).status_code == 404


def test_correction_request_validation(client):
    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]

    # base_revision 为负 → 422
    resp = client.post(
        _correction_url(eval_id),
        json={"base_revision": -1, "request_no": "x", "decision": {"v": 1}},
    )
    assert resp.status_code == 422

    # 纯空白请求号 → 422
    resp = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "   ", "decision": {"v": 1}},
    )
    assert resp.status_code == 422

    # 替代 decision 不是对象（数组/标量）→ 422
    resp = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "x", "decision": [1, 2]},
    )
    assert resp.status_code == 422
    resp = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "x", "decision": "not-object"},
    )
    assert resp.status_code == 422

    assert client.get(_correction_url(eval_id)).json()["items"] == []


def test_empty_decision_object_allowed(client):
    """完整替代 decision 允许为空对象（人工清空全部决策字段）。"""
    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]
    resp = client.post(
        _correction_url(eval_id),
        json={"base_revision": 0, "request_no": "empty", "decision": {}},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["decision"] == {}
    assert (
        client.get(f"/api/evaluations/{eval_id}/effective-decision").json()["decision"]
        == {}
    )


# ---------- 并发 ----------


def test_concurrent_same_request_single_correction(client, monkeypatch):
    """同决策同请求号并发：只有一个真正写入，另一个回放首次记录。"""
    import app.crud as crud_module

    _rid, evaluation = _create_ruleset_and_evaluate(client)
    eval_id = evaluation["id"]

    real_new_id = crud_module.new_id
    insert_started = threading.Event()
    release_insert = threading.Event()

    def blocking_new_id():
        insert_started.set()
        release_insert.wait(timeout=10)
        return real_new_id()

    monkeypatch.setattr(crud_module, "new_id", blocking_new_id)

    payload = {"base_revision": 0, "request_no": "concurrent",
               "decision": {"v": 1}}
    outcome: list[dict] = []
    errors: list = []

    def call():
        try:
            resp = client.post(_correction_url(eval_id), json=payload)
            outcome.append({"status": resp.status_code, "body": resp.json()})
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    t1 = threading.Thread(target=call)
    t1.start()
    assert insert_started.wait(timeout=10)
    t2 = threading.Thread(target=call)
    t2.start()
    time.sleep(0.5)
    release_insert.set()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert errors == [], errors
    assert len(outcome) == 2
    assert {o["status"] for o in outcome} == {200, 201}, outcome
    ids = {o["body"]["id"] for o in outcome}
    assert len(ids) == 1
    assert len(client.get(_correction_url(eval_id)).json()["items"]) == 1
