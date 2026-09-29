"""POST /api/rulesets/{id}/input-footprint 接口测试。"""


def test_footprint_demo_is_vip(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ruleset_id": demo_id,
        "version": 1,
        "rule_codes": ["is_vip"],
        "footprints": [
            {
                "rule_code": "is_vip",
                "paths": [
                    {"path": "/customer/vip", "used_by": ["is_vip"]},
                ],
            }
        ],
    }


def test_footprint_demo_vip_discount_includes_upstream(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["vip_discount"]},
    )
    body = resp.json()
    fp = body["footprints"][0]
    # 路径按 JSON Pointer 字典序
    assert [p["path"] for p in fp["paths"]] == ["/amount", "/customer/vip"]
    users = {p["path"]: p["used_by"] for p in fp["paths"]}
    # 自身读 /amount；上游 is_vip 读 /customer/vip
    assert users["/amount"] == ["vip_discount"]
    assert users["/customer/vip"] == ["is_vip"]


def test_footprint_demo_manual_review_merged_upstreams(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["manual_review"]},
    )
    users = {
        p["path"]: p["used_by"]
        for p in resp.json()["footprints"][0]["paths"]
    }
    # /amount 同时被上游 vip_discount 与自身读取，按 evaluation_order 排列
    assert users == {
        "/amount": ["vip_discount", "manual_review"],
        "/customer/vip": ["is_vip"],
    }


def test_footprint_demo_free_shipping_and_coupons(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["free_shipping", "coupon_save20"]},
    )
    body = resp.json()
    assert body["rule_codes"] == ["free_shipping", "coupon_save20"]
    assert [f["rule_code"] for f in body["footprints"]] == [
        "free_shipping",
        "coupon_save20",
    ]
    shipping, coupon = body["footprints"]
    assert {
        p["path"]: p["used_by"] for p in shipping["paths"]
    } == {
        "/amount": ["large_order", "free_shipping"],
        "/region": ["free_shipping"],
    }
    assert coupon["paths"] == [{"path": "/coupons", "used_by": ["coupon_save20"]}]


def test_footprint_dedup_rule_codes(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["is_vip", "is_vip"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rule_codes"] == ["is_vip"]
    assert [f["rule_code"] for f in body["footprints"]] == ["is_vip"]


def test_footprint_condition_and_action_union(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "fp",
            "rules": [
                {
                    "code": "r1",
                    "condition": 'doc["cond"] == true',
                    "action": '{"x": doc["out"]}',
                }
            ],
        },
    ).json()["ruleset_id"]
    body = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["r1"]},
    ).json()
    assert [p["path"] for p in body["footprints"][0]["paths"]] == [
        "/cond",
        "/out",
    ]


def test_footprint_integer_index_and_rfc6901_escaping(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "fp",
            "rules": [
                {
                    "code": "r1",
                    "condition": (
                        'doc["a/b"] == 1 and doc["x~y"][0] == 2 '
                        'and doc[3] == 3 and doc[""] == 4'
                    ),
                }
            ],
        },
    ).json()["ruleset_id"]
    body = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["r1"]},
    ).json()
    # RFC 6901 转义后字典序："" -> "/" -> "/3" -> "/a~1b" -> "/x~0y/0"
    assert [p["path"] for p in body["footprints"][0]["paths"]] == [
        "/",
        "/3",
        "/a~1b",
        "/x~0y/0",
    ]


def test_footprint_uses_version_snapshot(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()[
        "ruleset_id"
    ]
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "simple",
            "rules": [
                {"code": "r1", "condition": 'doc["y"] >= 1000'},
                {"code": "r3"},
            ],
        },
    )
    v1 = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["r2"]},
    ).json()
    # v1: r2 上游为 r1，r1 读 /x；r2 自身不读文档
    assert v1["footprints"][0]["paths"] == [
        {"path": "/x", "used_by": ["r1"]}
    ]
    v2 = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 2, "rule_codes": ["r1"]},
    ).json()
    assert [p["path"] for p in v2["footprints"][0]["paths"]] == ["/y"]


def test_footprint_dynamic_index_returns_422(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "fp",
            "rules": [
                {"code": "r1", "condition": 'doc["k" + "x"] == 1'},
            ],
        },
    ).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["r1"]},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert error["details"]["rule_code"] == "r1"
    assert error["details"]["part"] == "condition"
    assert error["details"]["index"] == '"k" + "x"'
    # 不得给出不完整结果
    assert "footprints" not in error


def test_footprint_dynamic_index_variants_all_422(client):
    variants = [
        "doc[-1]",
        "doc[1:3]",
        "doc[True]",
        "doc[None]",
    ]
    for expr in variants:
        rid = client.post(
            "/api/rulesets",
            json={"name": "fp", "rules": [{"code": "r1", "condition": expr}]},
        ).json()["ruleset_id"]
        resp = client.post(
            f"/api/rulesets/{rid}/input-footprint",
            json={"version": 1, "rule_codes": ["r1"]},
        )
        assert resp.status_code == 422, expr
        assert (
            resp.json()["error"]["code"] == "UNSUPPORTED_DOCUMENT_ACCESS"
        ), expr


def test_footprint_dynamic_index_in_upstream_returns_422(client):
    rid = client.post(
        "/api/rulesets",
        json={
            "name": "fp",
            "rules": [
                {"code": "up", "condition": 'doc[1:2] == "x"'},
                {
                    "code": "down",
                    "condition": "up.hit and doc['ok']",
                    "depends_on": ["up"],
                },
            ],
        },
    ).json()["ruleset_id"]
    # 目标本身静态安全，但上游含动态下标 -> 整体 422
    resp = client.post(
        f"/api/rulesets/{rid}/input-footprint",
        json={"version": 1, "rule_codes": ["down"]},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert error["details"]["rule_code"] == "up"


def test_footprint_does_not_write_evaluations(client, demo_id):
    before = client.get(f"/api/rulesets/{demo_id}/evaluations").json()
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 200
    after = client.get(f"/api/rulesets/{demo_id}/evaluations").json()
    assert before == after == []


def test_footprint_ruleset_not_found(client):
    resp = client.post(
        "/api/rulesets/nope/input-footprint",
        json={"version": 1, "rule_codes": ["a"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_footprint_version_not_found(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 99, "rule_codes": ["is_vip"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"


def test_footprint_unknown_rule_code_422(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/input-footprint",
        json={"version": 1, "rule_codes": ["is_vip", "nope"]},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "RULE_NOT_FOUND"
    assert error["details"] == {"rule_codes": ["nope"]}


def test_footprint_validates_request_body(client, demo_id):
    # version 必填
    assert (
        client.post(
            f"/api/rulesets/{demo_id}/input-footprint",
            json={"rule_codes": ["is_vip"]},
        ).status_code
        == 422
    )
    # version 必须 >= 1
    assert (
        client.post(
            f"/api/rulesets/{demo_id}/input-footprint",
            json={"version": 0, "rule_codes": ["is_vip"]},
        ).status_code
        == 422
    )
    # rule_codes 必填
    assert (
        client.post(
            f"/api/rulesets/{demo_id}/input-footprint",
            json={"version": 1},
        ).status_code
        == 422
    )
    # rule_codes 非空
    assert (
        client.post(
            f"/api/rulesets/{demo_id}/input-footprint",
            json={"version": 1, "rule_codes": []},
        ).status_code
        == 422
    )
