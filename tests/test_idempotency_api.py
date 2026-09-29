"""单文档决策的客户端幂等键测试。"""

import threading
import time

from app.database import connect
from app import crud

EVALUATE = "/api/rulesets/{rid}/evaluate"


def _create_ruleset(client, payload=None):
    payload = payload or {
        "name": "idem",
        "rules": [
            {"code": "r1", "condition": 'doc["x"] >= 10',
             "action": '{"level": "high"}'},
            {"code": "r2", "condition": "r1.hit", "depends_on": ["r1"],
             "action": '{"bonus": 1}'},
        ],
    }
    resp = client.post("/api/rulesets", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["ruleset_id"]


# ---------- 基础回放 ----------


def test_idempotent_key_first_call_persists(client):
    rid = _create_ruleset(client)
    resp = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "key-001"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"]  # 首次受理，正常落库拿到记录 ID
    assert body["version"] == 1
    assert body["decision"] == {"level": "high", "bonus": 1}

    # 幂等行与决策结果都已落库并关联
    conn = connect()
    try:
        row = crud.get_idempotency(conn, rid, "key-001")
        assert row is not None
        assert row["version_mode"] == "latest"
        assert row["pinned_version"] is None
        assert row["evaluation_id"] == body["id"]
    finally:
        conn.close()


def test_idempotent_key_replay_returns_first_record(client):
    rid = _create_ruleset(client)
    first = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "k"},
    ).json()

    second = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "k"},
    )
    assert second.status_code == 200
    replay = second.json()
    # 同键同请求：返回首次结果与原记录 ID，响应结构不变
    assert replay["id"] == first["id"]
    assert replay["version"] == first["version"]
    assert replay["decision"] == first["decision"]
    assert replay["created_at"] == first["created_at"]

    # 只落了一条决策、一条幂等记录
    listing = client.get(f"/api/rulesets/{rid}/evaluations").json()
    assert [e["id"] for e in listing] == [first["id"]]


def test_idempotent_replay_pins_version_even_after_new_release(client):
    """首次用最新版受理；之后发布新版本，latest 重试仍返回首次结果。"""
    rid = _create_ruleset(client)
    first = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 100}},
        headers={"Idempotency-Key": "pin-k"},
    ).json()
    assert first["version"] == 1
    assert first["fired_rule_codes"] == ["r1", "r2"]

    # 收紧阈值并发布 v2
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "idem",
            "rules": [{"code": "r1", "condition": 'doc["x"] >= 1000',
                       "action": '{"level": "high"}'}],
        },
    )

    # 不带版本选择（latest）重试：固定在首次的 v1，而不是当前 v2
    replay = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 100}},
        headers={"Idempotency-Key": "pin-k"},
    )
    assert replay.status_code == 200
    body = replay.json()
    assert body["id"] == first["id"]
    assert body["version"] == 1
    assert body["fired_rule_codes"] == ["r1", "r2"]
    # 没有产生新决策
    assert len(client.get(f"/api/rulesets/{rid}/evaluations").json()) == 1


def test_idempotent_replay_pinned_explicit_version(client):
    rid = _create_ruleset(client)
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "idem",
            "rules": [{"code": "r1", "condition": 'doc["x"] >= 1000',
                       "action": '{"level": "high"}'}],
        },
    )
    first = client.post(
        EVALUATE.format(rid=rid) + "?version=1",
        json={"document": {"x": 100}},
        headers={"Idempotency-Key": "pk"},
    ).json()
    assert first["version"] == 1

    replay = client.post(
        EVALUATE.format(rid=rid) + "?version=1",
        json={"document": {"x": 100}},
        headers={"Idempotency-Key": "pk"},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == first["id"]
    assert replay.json()["version"] == 1


# ---------- 冲突 ----------


def test_idempotent_conflict_on_different_document(client):
    rid = _create_ruleset(client)
    client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "dup"},
    )
    resp = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 1}},
        headers={"Idempotency-Key": "dup"},
    )
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert error["details"]["field"] == "document"

    # 冲突不产生新决策
    assert len(client.get(f"/api/rulesets/{rid}/evaluations").json()) == 1


def test_idempotent_conflict_on_version_selection_mode(client):
    rid = _create_ruleset(client)
    client.post(
        EVALUATE.format(rid=rid) + "?version=1",
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "vk"},
    )
    # 同一解析版本（v1 也是最新版），但选择方式不同（latest vs pinned）→ 冲突
    resp = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "vk"},
    )
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert error["details"]["field"] == "version_selection"
    assert error["details"]["original"] == {"mode": "pinned", "version": 1}
    assert error["details"]["conflicting"] == {"mode": "latest", "version": None}


def test_idempotent_conflict_on_different_pinned_version(client):
    rid = _create_ruleset(client)
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "idem",
            "rules": [{"code": "r1", "condition": 'doc["x"] >= 1000',
                       "action": '{"level": "high"}'}],
        },
    )
    client.post(
        EVALUATE.format(rid=rid) + "?version=1",
        json={"document": {"x": 100}},
        headers={"Idempotency-Key": "vv"},
    )
    resp = client.post(
        EVALUATE.format(rid=rid) + "?version=2",
        json={"document": {"x": 100}},
        headers={"Idempotency-Key": "vv"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["field"] == "version_selection"


def test_idempotent_document_canonicalized_by_sorted_object_keys(client):
    """对象键序不同的等价文档视为相同请求；数组顺序是语义，不规范化。"""
    rid = _create_ruleset(client)
    first = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"a": 1, "b": {"y": 2, "x": 1}}},
        headers={"Idempotency-Key": "canon"},
    )
    assert first.status_code == 200

    # 键顺序不同 → 规范化后相同，回放首次结果
    resp = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"b": {"x": 1, "y": 2}, "a": 1}},
        headers={"Idempotency-Key": "canon"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == first.json()["id"]

    # 数组顺序不同 → 文档不同，冲突
    resp = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"a": 1, "b": [2, 1]}},
        headers={"Idempotency-Key": "canon"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["field"] == "document"


# ---------- 失败不占键 / 非持久化拒绝 / 无键行为不变 ----------


def test_failed_evaluation_does_not_consume_key(client, monkeypatch):
    from app.engine import EvaluationFailed
    import app.main as main_module

    rid = _create_ruleset(client)
    real_evaluate = main_module.evaluate
    calls = {"n": 0}

    def flaky(compiled, document):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EvaluationFailed("NOT_CONVERGED", "模拟不收敛",
                                   {"max_iterations": 100})
        return real_evaluate(compiled, document)

    monkeypatch.setattr(main_module, "evaluate", flaky)

    # 首次求值失败：422，键未被占用
    resp = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "retry-k"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "NOT_CONVERGED"

    conn = connect()
    try:
        assert crud.get_idempotency(conn, rid, "retry-k") is None
    finally:
        conn.close()

    # 同一键可被后续成功请求重新使用（没有被失败“占坑”）
    resp2 = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "retry-k"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["id"]
    assert len(client.get(f"/api/rulesets/{rid}/evaluations").json()) == 1


def test_idempotency_key_rejected_for_non_persistent_call(client):
    rid = _create_ruleset(client)
    resp = client.post(
        EVALUATE.format(rid=rid) + "?persist=false",
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "nk"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRES_PERSIST"
    # 被拒绝：不落库、不占键
    assert client.get(f"/api/rulesets/{rid}/evaluations").json() == []
    conn = connect()
    try:
        assert crud.get_idempotency(conn, rid, "nk") is None
    finally:
        conn.close()


def test_blank_idempotency_key_treated_as_absent(client):
    # 纯空白头经 HTTP 传输规范化后等同未携带：走无键路径，正常落库
    rid = _create_ruleset(client)
    resp = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "   "},
    )
    assert resp.status_code == 200
    assert resp.json()["id"]
    conn = connect()
    try:
        assert crud.get_idempotency(conn, rid, "   ") is None
    finally:
        conn.close()


def test_too_long_idempotency_key_rejected(client):
    rid = _create_ruleset(client)
    resp = client.post(
        EVALUATE.format(rid=rid),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "k" * 256},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_IDEMPOTENCY_KEY"


def test_request_without_key_keeps_existing_behavior(client):
    rid = _create_ruleset(client)
    r1 = client.post(
        EVALUATE.format(rid=rid), json={"document": {"x": 42}}
    ).json()
    r2 = client.post(
        EVALUATE.format(rid=rid), json={"document": {"x": 42}}
    ).json()
    # 无键：每次都重新求值并落库，得到不同记录 ID
    assert r1["id"] and r2["id"]
    assert r1["id"] != r2["id"]
    assert len(client.get(f"/api/rulesets/{rid}/evaluations").json()) == 2


def test_key_scoped_per_ruleset(client):
    rid1 = _create_ruleset(client)
    rid2 = _create_ruleset(client)
    a = client.post(
        EVALUATE.format(rid=rid1),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "shared"},
    ).json()
    b = client.post(
        EVALUATE.format(rid=rid2),
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "shared"},
    )
    assert b.status_code == 200
    assert b.json()["id"] != a["id"]
    assert b.json()["ruleset_id"] == rid2


# ---------- 错误语义保留 ----------


def test_idempotent_key_ruleset_not_found(client):
    resp = client.post(
        EVALUATE.format(rid="nope"),
        json={"document": {}},
        headers={"Idempotency-Key": "x"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_idempotent_key_pinned_version_not_found(client):
    rid = _create_ruleset(client)
    resp = client.post(
        EVALUATE.format(rid=rid) + "?version=99",
        json={"document": {"x": 42}},
        headers={"Idempotency-Key": "x"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"
    conn = connect()
    try:
        assert crud.get_idempotency(conn, rid, "x") is None
    finally:
        conn.close()


# ---------- 并发 ----------


def test_concurrent_same_key_single_evaluation(client, monkeypatch):
    """同键并发请求：只有一个真正求值落库，另一个复用首次结果。

    在 save_evaluation（已持写锁的事务内）插入阻塞点，保证第二个请求
    先在 BEGIN IMMEDIATE 上排队；第一个提交后它拿到锁、复查命中并回放，
    而不是重复求值落库。
    """
    import app.crud as crud_module

    rid = _create_ruleset(client)
    real_save = crud_module.save_evaluation
    save_started = threading.Event()
    release_save = threading.Event()

    def blocking_save(conn, ruleset_id, version, document, result):
        save_started.set()
        # 等待主线程确认第二个请求已发起并在写锁上排队
        release_save.wait(timeout=10)
        return real_save(conn, ruleset_id, version, document, result)

    monkeypatch.setattr(crud_module, "save_evaluation", blocking_save)

    outcome: list[dict] = []
    errors: list = []

    def call():
        try:
            resp = client.post(
                EVALUATE.format(rid=rid),
                json={"document": {"x": 42}},
                headers={"Idempotency-Key": "concurrent-k"},
            )
            outcome.append({"status": resp.status_code, "body": resp.json()})
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    t1 = threading.Thread(target=call)
    t1.start()
    # 等第一个请求进入写锁事务
    assert save_started.wait(timeout=10)
    t2 = threading.Thread(target=call)
    t2.start()
    # 给第二个请求时间完成无锁快查并阻塞在 BEGIN IMMEDIATE
    time.sleep(0.5)
    release_save.set()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert errors == [], errors
    assert len(outcome) == 2
    assert all(o["status"] == 200 for o in outcome), outcome
    ids = {o["body"]["id"] for o in outcome}
    assert len(ids) == 1  # 同一首次记录 ID
    assert len(client.get(f"/api/rulesets/{rid}/evaluations").json()) == 1

    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM idempotency_keys WHERE key = ?",
            ("concurrent-k",),
        ).fetchone()
        assert row["c"] == 1
    finally:
        conn.close()
