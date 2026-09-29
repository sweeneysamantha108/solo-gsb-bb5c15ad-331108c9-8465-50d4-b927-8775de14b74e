"""POST /api/rulesets/{id}/impact 接口测试。"""


def test_impact_demo_ruleset(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/impact",
        json={"version": 1, "rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ruleset_id"] == demo_id
    assert body["version"] == 1
    assert body["rule_codes"] == ["is_vip"]

    # 层级：is_vip(0) -> vip_discount(1) -> manual_review(2)
    assert body["direct_dependents"] == ["vip_discount"]
    assert body["transitive_dependents"] == ["manual_review"]
    assert [item["rule_code"] for item in body["affected"]] == [
        "is_vip",
        "vip_discount",
        "manual_review",
    ]
    assert body["affected"] == [
        {"rule_code": "is_vip", "level": 0, "relation": "input"},
        {"rule_code": "vip_discount", "level": 1, "relation": "direct_dependent"},
        {"rule_code": "manual_review", "level": 2, "relation": "transitive_dependent"},
    ]

    # discount 组：vip_discount 在闭包中，large_order 标记为联动
    assert body["coupled_rules"] == [
        {
            "rule_code": "large_order",
            "conflict_group": "discount",
            "coupled_with": "vip_discount",
            "level": None,
        }
    ]


def test_impact_multiple_rule_codes(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/impact",
        json={"version": 1, "rule_codes": ["large_order", "is_vip"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rule_codes"] == ["large_order", "is_vip"]
    # free_shipping 直接依赖 large_order；
    # vip_discount 直接依赖 is_vip（其同组 large_order 已在输入中，不算联动）；
    # manual_review 为传递依赖（经由 vip_discount）
    assert body["direct_dependents"] == ["vip_discount", "free_shipping"]
    assert body["transitive_dependents"] == ["manual_review"]
    # 闭包已覆盖 discount 组双方；coupon 组与闭包无关 -> 无联动项
    assert body["coupled_rules"] == []

    relations = {item["rule_code"]: item["relation"] for item in body["affected"]}
    assert relations["large_order"] == "input"
    assert relations["is_vip"] == "input"
    assert relations["free_shipping"] == "direct_dependent"


def test_impact_leaf_rule_has_no_dependents(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/impact",
        json={"version": 1, "rule_codes": ["manual_review"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [item["rule_code"] for item in body["affected"]] == ["manual_review"]
    assert body["direct_dependents"] == []
    assert body["transitive_dependents"] == []
    assert body["coupled_rules"] == []


def test_impact_does_not_write_evaluations(client, demo_id):
    before = client.get(f"/api/rulesets/{demo_id}/evaluations").json()
    resp = client.post(
        f"/api/rulesets/{demo_id}/impact",
        json={"version": 1, "rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 200
    after = client.get(f"/api/rulesets/{demo_id}/evaluations").json()
    assert before == after == []


def test_impact_uses_specific_version_snapshot(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "simple",
            "rules": [
                {"code": "r1", "condition": 'doc["x"] >= 1000'},
                {"code": "r3"},
            ],
        },
    )
    # v1：r1 -> r2；v2 中 r2 已不存在
    v1 = client.post(
        f"/api/rulesets/{rid}/impact",
        json={"version": 1, "rule_codes": ["r1"]},
    ).json()
    assert v1["direct_dependents"] == ["r2"]
    v2 = client.post(
        f"/api/rulesets/{rid}/impact",
        json={"version": 2, "rule_codes": ["r1"]},
    ).json()
    assert v2["direct_dependents"] == []
    assert [a["rule_code"] for a in v2["affected"]] == ["r1"]


def test_impact_ruleset_not_found(client):
    resp = client.post(
        "/api/rulesets/nope/impact",
        json={"version": 1, "rule_codes": ["a"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_impact_version_not_found(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/impact",
        json={"version": 99, "rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"


def test_impact_unknown_rule_code_422(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/impact",
        json={"version": 1, "rule_codes": ["is_vip", "nope"]},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "RULE_NOT_FOUND"
    assert error["details"] == {"rule_codes": ["nope"]}


def test_impact_validates_request_body(client, demo_id):
    # version 必须 >= 1
    resp = client.post(
        f"/api/rulesets/{demo_id}/impact",
        json={"version": 0, "rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 422

    # rule_codes 不能为空
    resp = client.post(
        f"/api/rulesets/{demo_id}/impact",
        json={"version": 1, "rule_codes": []},
    )
    assert resp.status_code == 422

    # 缺少必填字段
    resp = client.post(
        f"/api/rulesets/{demo_id}/impact",
        json={"rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 422
