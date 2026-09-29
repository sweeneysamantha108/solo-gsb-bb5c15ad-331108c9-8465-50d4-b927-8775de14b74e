"""规则集快照迁移接口测试：导出、摘要核对、重编译、幂等导入与原子化。"""

import hashlib
import json

from app import crud


def _create_ruleset(client, payload, rid="src-set"):
    body = {"id": rid, **payload}
    resp = client.post("/api/rulesets", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _export(client, rid, version):
    resp = client.get(f"/api/rulesets/{rid}/versions/{version}/snapshot")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_export_snapshot_package_shape(client, simple_ruleset_payload):
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    pkg = _export(client, "src-set", 1)

    assert pkg["format"] == "ruleset-snapshot"
    assert pkg["format_version"] == 1
    assert pkg["source"] == {"ruleset_id": "src-set", "version": 1}
    assert pkg["exported_at"]
    assert pkg["digest"]["algorithm"] == "sha256"
    assert len(pkg["digest"]["value"]) == 64
    body = pkg["ruleset"]
    assert body["name"] == "simple"
    assert [r["code"] for r in body["rules"]] == ["r1", "r2"]
    # 规则完整定义随包导出
    assert body["rules"][0]["condition"] == 'doc["x"] >= 10'


def test_export_404(client, simple_ruleset_payload):
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    assert client.get("/api/rulesets/missing/versions/1/snapshot").status_code == 404
    assert client.get(
        "/api/rulesets/src-set/versions/9/snapshot"
    ).status_code == 404


def test_digest_matches_canonical_content(client, simple_ruleset_payload):
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    pkg = _export(client, "src-set", 1)
    canonical = json.dumps(
        {
            "name": "simple",
            "description": "",
            "default_conflict_policy": "collect_all",
            "rules": [
                {
                    "code": "r1",
                    "name": "r1",
                    "description": "",
                    "priority": 0,
                    "conflict_group": None,
                    "conflict_policy": None,
                    "depends_on": [],
                    "condition": 'doc["x"] >= 10',
                    "action": '{"level": "high"}',
                },
                {
                    "code": "r2",
                    "name": "r2",
                    "description": "",
                    "priority": 0,
                    "conflict_group": None,
                    "conflict_policy": None,
                    "depends_on": ["r1"],
                    "condition": "r1.hit",
                    "action": '{"bonus": 1}',
                },
            ],
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert pkg["digest"]["value"] == hashlib.sha256(canonical).hexdigest()


def test_import_creates_v1_for_missing_target(client, simple_ruleset_payload):
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    pkg = _export(client, "src-set", 1)

    resp = client.post("/api/rulesets/dst-set/snapshot-import", json=pkg)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["action"] == "created"
    assert out["ruleset_id"] == "dst-set"
    assert out["version"] == 1
    assert out["source"] == {"ruleset_id": "src-set", "version": 1}
    assert out["digest"] == pkg["digest"]
    snap = out["snapshot"]
    assert snap["ruleset_id"] == "dst-set"
    assert snap["version"] == 1
    assert [r["code"] for r in snap["rules"]] == ["r1", "r2"]
    assert snap["evaluation_order"] == ["r1", "r2"]

    # 导入后可正常求值
    eval_resp = client.post(
        "/api/rulesets/dst-set/evaluate", json={"document": {"x": 42}}
    )
    assert eval_resp.status_code == 200
    assert eval_resp.json()["decision"] == {"level": "high", "bonus": 1}


def test_import_same_content_is_idempotent_match(client, simple_ruleset_payload):
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    pkg = _export(client, "src-set", 1)

    first = client.post("/api/rulesets/dst-set/snapshot-import", json=pkg)
    assert first.status_code == 201
    first_id = first.json()["snapshot"]["id"]

    # 二次导入同一内容：matched，不产生新版本
    second = client.post("/api/rulesets/dst-set/snapshot-import", json=pkg)
    assert second.status_code == 200
    assert second.json()["action"] == "matched"
    assert second.json()["version"] == 1
    assert second.json()["snapshot"]["id"] == first_id

    meta = client.get("/api/rulesets/dst-set").json()
    assert meta["current_version"] == 1


def test_import_package_version_ignored_target_versions_increment(
    client, simple_ruleset_payload
):
    # 源规则集推进到 v2，导出 v2
    rid = _create_ruleset(client, simple_ruleset_payload, rid="src-set")[
        "ruleset_id"
    ]
    v2_payload = dict(simple_ruleset_payload)
    v2_payload["rules"] = [
        {
            "code": "r1",
            "condition": 'doc["x"] >= 100',
            "action": '{"level": "very-high"}',
        }
    ]
    assert client.put(f"/api/rulesets/{rid}", json=v2_payload).status_code == 200
    pkg_v2 = _export(client, "src-set", 2)
    assert pkg_v2["source"]["version"] == 2

    # 目标先导入源 v1（创建 v1），再导入源 v2：追加为目标 v2 而非沿用包内 2→无影响，
    # 且若先导入源 v2 再导入源 v1，包内 version=1 也不会覆盖，而是追加为目标 v2
    client.post("/api/rulesets/dst-set/snapshot-import", json=_export(client, "src-set", 1))
    resp = client.post("/api/rulesets/dst-set/snapshot-import", json=pkg_v2)
    assert resp.status_code == 200
    assert resp.json()["action"] == "appended"
    assert resp.json()["version"] == 2
    assert client.get("/api/rulesets/dst-set").json()["current_version"] == 2

    # 逆序导入：新目标先导入源 v2 → created v1；再导入源 v1 → 包内版本为 1，
    # 但内容不同，应追加为目标 v2（不采用包内版本号）
    other = client.post(
        "/api/rulesets/other-set/snapshot-import", json=pkg_v2
    ).json()
    assert other["action"] == "created" and other["version"] == 1
    resp2 = client.post(
        "/api/rulesets/other-set/snapshot-import",
        json=_export(client, "src-set", 1),
    )
    assert resp2.status_code == 200
    assert resp2.json()["action"] == "appended"
    assert resp2.json()["version"] == 2
    assert client.get("/api/rulesets/other-set").json()["current_version"] == 2
    assert [r["code"] for r in resp2.json()["snapshot"]["rules"]] == ["r1", "r2"]


def test_import_matches_any_version_not_only_latest(client, simple_ruleset_payload):
    rid = client.post(
        "/api/rulesets", json={"id": "dst-set", **simple_ruleset_payload}
    ).json()["ruleset_id"]
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "simple",
            "rules": [
                {"code": "r1", "condition": 'doc["x"] >= 100', "action": "{}"}
            ],
        },
    )
    # 目标当前为 v2，内容不同；导入源 v1 的内容，应匹配到历史 v1
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    pkg = _export(client, "src-set", 1)
    resp = client.post("/api/rulesets/dst-set/snapshot-import", json=pkg)
    assert resp.status_code == 200
    assert resp.json()["action"] == "matched"
    assert resp.json()["version"] == 1
    assert client.get("/api/rulesets/dst-set").json()["current_version"] == 2


def test_import_digest_mismatch_rejected_and_not_written(
    client, simple_ruleset_payload
):
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    pkg = _export(client, "src-set", 1)
    pkg["digest"]["value"] = "0" * 64

    resp = client.post("/api/rulesets/dst-set/snapshot-import", json=pkg)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DIGEST_MISMATCH"
    # 校验失败不写库
    assert client.get("/api/rulesets/dst-set").status_code == 404


def test_import_tampered_content_rejected(client, simple_ruleset_payload):
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    pkg = _export(client, "src-set", 1)
    pkg["ruleset"]["rules"][0]["condition"] = 'doc["x"] >= 1'

    resp = client.post("/api/rulesets/dst-set/snapshot-import", json=pkg)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DIGEST_MISMATCH"
    assert client.get("/api/rulesets/dst-set").status_code == 404


def test_import_recompiles_cyclic_ruleset_without_writing(
    client, simple_ruleset_payload
):
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    pkg = _export(client, "src-set", 1)
    # 用内容相同但自造循环的包替换规则定义，并重算一个“自洽”摘要，
    # 以证明重编译独立于摘要生效
    cyclic_rules = [
        {"code": "a", "condition": "b.hit", "depends_on": ["b"], "action": "{}"},
        {"code": "b", "condition": "a.hit", "depends_on": ["a"], "action": "{}"},
    ]
    pkg["ruleset"]["rules"] = cyclic_rules
    # 按服务端规范化口径重算一个与篡改内容自洽的摘要，
    # 以证明重编译独立于摘要生效
    content = crud.canonical_ruleset_content(pkg["ruleset"])
    pkg["digest"]["value"] = crud.content_digest(content)

    resp = client.post("/api/rulesets/dst-set/snapshot-import", json=pkg)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CYCLIC_DEPENDENCY"
    assert client.get("/api/rulesets/dst-set").status_code == 404


def test_import_invalid_target_id(client, simple_ruleset_payload):
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    pkg = _export(client, "src-set", 1)
    resp = client.post("/api/rulesets/bad%20id/snapshot-import", json=pkg)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ID"


def test_import_bad_format_rejected(client, simple_ruleset_payload):
    _create_ruleset(client, simple_ruleset_payload, rid="src-set")
    pkg = _export(client, "src-set", 1)

    bad_format = dict(pkg)
    bad_format["format"] = "something-else"
    resp = client.post("/api/rulesets/dst-set/snapshot-import", json=bad_format)
    assert resp.status_code == 422

    bad_algorithm = json.loads(json.dumps(pkg))
    bad_algorithm["digest"]["algorithm"] = "md5"
    resp = client.post("/api/rulesets/dst-set/snapshot-import", json=bad_algorithm)
    assert resp.status_code == 422


def test_round_trip_preserves_all_rule_fields(client):
    payload = {
        "name": "full-fields",
        "description": "desc",
        "default_conflict_policy": "priority",
        "rules": [
            {
                "code": "r1",
                "name": "规则一",
                "description": "d1",
                "priority": 5,
                "conflict_group": "g1",
                "conflict_policy": "priority",
                "depends_on": [],
                "condition": 'doc["x"] > 0',
                "action": '{"a": 1}',
            },
            {
                "code": "r2",
                "name": "规则二",
                "depends_on": ["r1"],
                "condition": "r1.hit",
                "action": '{"b": 2}',
            },
        ],
    }
    _create_ruleset(client, payload, rid="src-set")
    pkg = _export(client, "src-set", 1)
    resp = client.post("/api/rulesets/dst-set/snapshot-import", json=pkg)
    assert resp.status_code == 201
    snap = resp.json()["snapshot"]
    r1 = snap["rules"][0]
    assert r1["name"] == "规则一"
    assert r1["description"] == "d1"
    assert r1["priority"] == 5
    assert r1["conflict_group"] == "g1"
    assert r1["conflict_policy"] == "priority"
    assert snap["default_conflict_policy"] == "priority"
