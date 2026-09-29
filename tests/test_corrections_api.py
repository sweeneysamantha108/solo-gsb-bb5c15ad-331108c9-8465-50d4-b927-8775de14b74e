"""已落库决策的人工更正 API 测试。"""

import threading
import time


def _evaluate(client, rid, document=None):
    resp = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": document or {"x": 42}}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_ruleset_with_decision(client):
    resp = client.post(
        "/api/rulesets",
        json={
            "name": "corr",
            "rules": [
                {"code": "r1", "condition": 'doc["x"] >= 10',
                 "action": '{"level": "high"}'},
                {"code": "r2", "condition": "r1.hit", "depends_on": ["r1"],
                 "action": '{"bonus": 1}'},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    rid = resp.json()["ruleset_id"]
    decision_record = _evaluate(client, rid)
    return rid, decision_record


# ---------- 首次更正 ----------


def test_first_correction_is_revision_one(client):
    rid, ev = _create_ruleset_with_decision(client)
    assert ev["decision"] == {"level": "high", "bonus": 1}

    resp = client.post(
        f"/api/evaluations/{ev['id']}/corrections",
        json={
            "base_revision": 0,
            "request_no": "REV-001",
            "decision": {"level": "high", "bonus": 2, "reviewed": True},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["revision"] == 1
    assert body["base_revision"] == 0
    assert body["request_no"] == "REV-001"
    assert body["evaluation_id"] == ev["id"]
    assert body["id"]
    assert body["decision"] == {"level": "high", "bonus": 2, "reviewed": True}


def test_effective_decision_tracks_latest_correction(client):
    rid, ev = _create_ruleset_with_decision(client)
    eid = ev["id"]

    # 无更正：当前有效 decision 即原计算结果，修订 0
    eff0 = client.get(f"/api/evaluations/{eid}/effective-decision")
    assert eff0.status_code == 200
    assert eff0.json() == {
        "evaluation_id": eid,
        "ruleset_id": rid,
        "version": 1,
        "revision": 0,
        "decision": {"level": "high", "bonus": 1},
        "source": "original",
        "correction_id": None,
        "request_no": None,
        "corrected_at": None,
        "evaluation_created_at": ev["created_at"],
    }

    client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "R1",
              "decision": {"bonus": 2}},
    )
    eff1 = client.get(f"/api/evaluations/{eid}/effective-decision").json()
    assert eff1["revision"] == 1
    assert eff1["source"] == "correction"
    assert eff1["decision"] == {"bonus": 2}
    assert eff1["request_no"] == "R1"
    assert eff1["correction_id"]
    assert eff1["version"] == 1  # 所用规则版本不变

    # 第二次更正基于修订 1，成功后当前修订为 2
    resp = client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 1, "request_no": "R2",
              "decision": {"bonus": 3}},
    )
    assert resp.status_code == 201
    assert resp.json()["revision"] == 2

    eff2 = client.get(f"/api/evaluations/{eid}/effective-decision").json()
    assert eff2["revision"] == 2
    assert eff2["decision"] == {"bonus": 3}
    assert eff2["request_no"] == "R2"
    assert eff2["correction_id"] != eff1["correction_id"]


def test_corrections_listed_in_revision_order(client):
    _rid, ev = _create_ruleset_with_decision(client)
    eid = ev["id"]
    assert client.get(f"/api/evaluations/{eid}/corrections").json() == []

    client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "R1", "decision": {"a": 1}},
    )
    client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 1, "request_no": "R2", "decision": {"a": 2}},
    )

    records = client.get(f"/api/evaluations/{eid}/corrections").json()
    assert [r["revision"] for r in records] == [1, 2]
    assert [r["base_revision"] for r in records] == [0, 1]
    assert [r["request_no"] for r in records] == ["R1", "R2"]
    assert records[0]["decision"] == {"a": 1}
    assert records[1]["decision"] == {"a": 2}


def test_original_evaluation_and_stats_unchanged(client):
    rid, ev = _create_ruleset_with_decision(client)
    eid = ev["id"]
    original = client.get(f"/api/evaluations/{eid}").json()

    client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "R1",
              "decision": {"totally": "different"}},
    )
    client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 1, "request_no": "R2",
              "decision": {"another": "one"}},
    )

    after = client.get(f"/api/evaluations/{eid}").json()
    # 原始决策记录逐字段不变
    assert after == original
    assert after["decision"] == {"level": "high", "bonus": 1}
    assert after["version"] == 1

    # 评估统计仍基于原始结果：1 条评估、命中字段为原字段
    stats = client.get(f"/api/rulesets/{rid}/evaluation-stats").json()
    assert stats["total_evaluations"] == 1
    assert {f["field"] for f in stats["decision_field_counts"]} == {
        "level", "bonus"
    }

    # 历史列表仍只含原始一条评估
    listing = client.get(f"/api/rulesets/{rid}/evaluations").json()
    assert [e["id"] for e in listing] == [eid]


# ---------- 幂等重试 ----------


def test_same_request_no_replays_first_correction(client):
    _rid, ev = _create_ruleset_with_decision(client)
    eid = ev["id"]
    payload = {"base_revision": 0, "request_no": "DUP",
               "decision": {"bonus": 9}}
    first = client.post(f"/api/evaluations/{eid}/corrections", json=payload)
    assert first.status_code == 201
    first_body = first.json()

    # 完全相同的重试：200 回放首次记录
    retry = client.post(f"/api/evaluations/{eid}/corrections", json=payload)
    assert retry.status_code == 200
    assert retry.json() == first_body

    # 只写了一条更正
    records = client.get(f"/api/evaluations/{eid}/corrections").json()
    assert len(records) == 1
    assert records[0]["id"] == first_body["id"]


def test_replay_works_even_after_later_corrections(client):
    """已有后续更正后，同号同基准同内容的重试仍返回首次更正记录，不重复写入。"""
    _rid, ev = _create_ruleset_with_decision(client)
    eid = ev["id"]
    first = client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "R1", "decision": {"v": 1}},
    ).json()
    client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 1, "request_no": "R2", "decision": {"v": 2}},
    )
    # 当前修订已是 2；用旧基准 0 重试 R1 —— 不是过期冲突，而是回放首次记录
    retry = client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "R1", "decision": {"v": 1}},
    )
    assert retry.status_code == 200
    body = retry.json()
    assert body["id"] == first["id"]
    assert body["revision"] == 1
    # 仍只有两条更正，没有重复写入
    records = client.get(f"/api/evaluations/{eid}/corrections").json()
    assert [r["revision"] for r in records] == [1, 2]


def test_replay_decision_canonicalized_by_object_keys(client):
    """替代 decision 对象键序不同但语义相同视为同内容，回放首次记录。"""
    _rid, ev = _create_ruleset_with_decision(client)
    eid = ev["id"]
    first = client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "CANON",
              "decision": {"a": 1, "b": {"y": 2, "x": 1}}},
    )
    assert first.status_code == 201
    retry = client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "CANON",
              "decision": {"b": {"x": 1, "y": 2}, "a": 1}},
    )
    assert retry.status_code == 200
    assert retry.json()["id"] == first.json()["id"]
    assert len(client.get(f"/api/evaluations/{eid}/corrections").json()) == 1


def test_request_no_scoped_per_evaluation(client):
    _rid, ev1 = _create_ruleset_with_decision(client)
    ev2 = _evaluate(client, ev1["ruleset_id"], {"x": 42})
    payload = {"base_revision": 0, "request_no": "SHARED",
               "decision": {"v": 1}}
    a = client.post(f"/api/evaluations/{ev1['id']}/corrections", json=payload)
    b = client.post(f"/api/evaluations/{ev2['id']}/corrections", json=payload)
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


# ---------- 冲突 ----------


def test_stale_base_revision_conflict(client):
    _rid, ev = _create_ruleset_with_decision(client)
    eid = ev["id"]
    client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "R1", "decision": {"v": 1}},
    )
    # 仍以过期基准 0 发起“新”更正（新请求号）→ 冲突
    resp = client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "R2", "decision": {"v": 2}},
    )
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "STALE_BASE_REVISION"
    assert error["details"]["base_revision"] == 0
    assert error["details"]["current_revision"] == 1
    # 冲突不产生更正记录，当前修订仍为 1
    records = client.get(f"/api/evaluations/{eid}/corrections").json()
    assert [r["revision"] for r in records] == [1]
    assert client.get(
        f"/api/evaluations/{eid}/effective-decision"
    ).json()["decision"] == {"v": 1}


def test_same_request_no_different_base_conflict(client):
    _rid, ev = _create_ruleset_with_decision(client)
    eid = ev["id"]
    client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "R1", "decision": {"v": 1}},
    )
    resp = client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 1, "request_no": "R1", "decision": {"v": 1}},
    )
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "CORRECTION_BASE_CONFLICT"
    assert error["details"]["field"] == "base_revision"
    assert error["details"]["original"] == {"base_revision": 0}
    assert error["details"]["conflicting"] == {"base_revision": 1}
    assert len(client.get(f"/api/evaluations/{eid}/corrections").json()) == 1


def test_same_request_no_same_base_different_content_conflict(client):
    _rid, ev = _create_ruleset_with_decision(client)
    eid = ev["id"]
    client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "R1", "decision": {"v": 1}},
    )
    resp = client.post(
        f"/api/evaluations/{eid}/corrections",
        json={"base_revision": 0, "request_no": "R1", "decision": {"v": 2}},
    )
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "CORRECTION_CONTENT_CONFLICT"
    assert error["details"]["field"] == "decision"
    assert len(client.get(f"/api/evaluations/{eid}/corrections").json()) == 1


def test_negative_base_revision_rejected(client):
    _rid, ev = _create_ruleset_with_decision(client)
    resp = client.post(
        f"/api/evaluations/{ev['id']}/corrections",
        json={"base_revision": -1, "request_no": "X", "decision": {"v": 1}},
    )
    assert resp.status_code == 422


def test_blank_request_no_rejected(client):
    _rid, ev = _create_ruleset_with_decision(client)
    resp = client.post(
        f"/api/evaluations/{ev['id']}/corrections",
        json={"base_revision": 0, "request_no": "   ", "decision": {"v": 1}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST_NO"
    assert client.get(f"/api/evaluations/{ev['id']}/corrections").json() == []


def test_empty_decision_rejected(client):
    _rid, ev = _create_ruleset_with_decision(client)
    resp = client.post(
        f"/api/evaluations/{ev['id']}/corrections",
        json={"base_revision": 0, "request_no": "X", "decision": {}},
    )
    assert resp.status_code == 422


# ---------- 不存在 ----------


def test_correction_evaluation_not_found(client):
    resp = client.post(
        "/api/evaluations/deadbeef/corrections",
        json={"base_revision": 0, "request_no": "X", "decision": {"v": 1}},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_effective_decision_not_found(client):
    resp = client.get("/api/evaluations/deadbeef/effective-decision")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_corrections_list_not_found(client):
    resp = client.get("/api/evaluations/deadbeef/corrections")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ---------- 并发：同请求号只落一条 ----------


def test_concurrent_same_request_no_single_correction(client, monkeypatch):
    """同业务请求号并发：只有一个真正写入，另一个回放首次记录。"""
    from app import crud as crud_module

    _rid, ev = _create_ruleset_with_decision(client)
    eid = ev["id"]
    real_new_id = crud_module.new_id
    insert_started = threading.Event()
    release_insert = threading.Event()

    def blocking_new_id():
        insert_started.set()
        release_insert.wait(timeout=10)
        return real_new_id()

    monkeypatch.setattr(crud_module, "new_id", blocking_new_id)

    payload = {"base_revision": 0, "request_no": "CONC",
               "decision": {"v": 1}}
    outcome: list[dict] = []
    errors: list = []

    def call():
        try:
            resp = client.post(
                f"/api/evaluations/{eid}/corrections", json=payload
            )
            outcome.append({"status": resp.status_code, "body": resp.json()})
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    t1 = threading.Thread(target=call)
    t1.start()
    assert insert_started.wait(timeout=10)
    t2 = threading.Thread(target=call)
    t2.start()
    time.sleep(0.5)  # 第二个请求完成无锁快查并在写锁上排队
    release_insert.set()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert errors == [], errors
    assert len(outcome) == 2
    assert {o["status"] for o in outcome} == {200, 201}, outcome
    ids = {o["body"]["id"] for o in outcome}
    assert len(ids) == 1  # 同一条首次更正记录
    revisions = {o["body"]["revision"] for o in outcome}
    assert revisions == {1}
    assert len(client.get(f"/api/evaluations/{eid}/corrections").json()) == 1
