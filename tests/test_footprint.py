"""输入足迹静态分析：doc 访问链提取、RFC 6901 转义、上游闭包聚合。"""

import pytest

from app.engine import compile_ruleset
from app.engine.footprint import (
    FootprintAnalysisError,
    escape_token,
    extract_doc_paths,
    input_footprint,
    json_pointer,
)


def _rule(code, condition="", action=None, depends_on=None):
    raw = {"code": code, "condition": condition, "depends_on": depends_on or []}
    if action is not None:
        raw["action"] = action
    return raw


# ---------- 纯表达式提取 ----------


def test_escape_token_rfc6901():
    assert escape_token("a~b") == "a~0b"
    assert escape_token("a/b") == "a~1b"
    assert escape_token("~/") == "~0~1"
    # 顺序敏感：必须先转义 ~ 再转义 /
    assert escape_token("a~1b") == "a~01b"


def test_json_pointer_basic():
    assert json_pointer(("customer", "vip")) == "/customer/vip"
    assert json_pointer((0,)) == "/0"
    assert json_pointer(("items", 2, "sku")) == "/items/2/sku"
    assert json_pointer(()) == ""
    # 含转义字符的下标
    assert json_pointer(("a/b", "c~d")) == "/a~1b/c~0d"
    assert json_pointer(("",)) == "/"


def test_simple_string_subscript():
    assert extract_doc_paths('doc["amount"] >= 100') == {"/amount"}
    assert extract_doc_paths('doc["a"] == doc["b"]') == {"/a", "/b"}


def test_nested_chain_collects_full_path_only():
    # 中间导航节点不单独计入：完整链末端才是规则读取的输入字段
    assert extract_doc_paths('doc["customer"]["vip"] == true') == {
        "/customer/vip"
    }


def test_integer_indices_including_zero():
    assert extract_doc_paths('doc["items"][0] == 1') == {"/items/0"}
    assert extract_doc_paths('doc[12]') == {"/12"}


def test_dot_attribute_access_treated_as_string_index():
    # 求值期 doc.foo 等价于 doc["foo"]（见 ast_eval._attr）
    assert extract_doc_paths("doc.amount >= 1") == {"/amount"}
    assert extract_doc_paths('doc.customer.vip') == {"/customer/vip"}
    # 下标与点访问混用
    assert extract_doc_paths('doc["a"].b') == {"/a/b"}


def test_rfc6901_escaping_in_extracted_paths():
    assert extract_doc_paths('doc["a/b"]') == {"/a~1b"}
    assert extract_doc_paths('doc["x~y"]') == {"/x~0y"}
    assert extract_doc_paths('doc[""]') == {"/"}


def test_bare_doc_means_whole_document():
    assert extract_doc_paths("len(doc)") == {""}
    assert extract_doc_paths("bool(doc)") == {""}
    assert extract_doc_paths("doc") == {""}
    # 作为接收者出现时不再重复记文档根
    assert extract_doc_paths('len(doc["a"])') == {"/a"}


def test_string_method_call_does_not_record_method_name():
    assert extract_doc_paths('doc["name"].upper() == "A"') == {"/name"}
    assert extract_doc_paths('upper(doc["name"])') == {"/name"}
    assert extract_doc_paths('doc["s"].startswith("x")') == {"/s"}


def test_unexecuted_branches_still_counted():
    # 保守 over-approximation：三元两个分支、and/or 两侧都计入
    assert extract_doc_paths('doc["a"] if doc["cond"] else doc["b"]') == {
        "/a",
        "/b",
        "/cond",
    }
    assert extract_doc_paths('doc["a"] and doc["b"]') == {"/a", "/b"}


def test_rule_references_are_not_doc_paths():
    # is_vip.hit / is_vip.value 属于规则引用，不计文档路径；
    # 规则结果上的下标不以 doc 为根，也不触发动态下标报错
    assert extract_doc_paths("is_vip.hit and r1.value[0] > 0") == set()


def test_dynamic_subscript_name_is_unsupported():
    # 仅含常量运算、无自由名字（裸名字会被编译器当作未声明规则引用拦截）
    with pytest.raises(FootprintAnalysisError) as exc:
        extract_doc_paths('doc["k" + "x"]')
    assert exc.value.code == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert exc.value.details["index"] == '"k" + "x"'


def test_dynamic_subscript_forms_all_rejected():
    for expr in [
        "doc[-1]",
        "doc[1:3]",
        "doc[1.5]",
        "doc[True]",
        "doc[None]",
        'doc[("a",)]',
    ]:
        with pytest.raises(FootprintAnalysisError) as exc:
            extract_doc_paths(expr)
        assert exc.value.code == "UNSUPPORTED_DOCUMENT_ACCESS", expr


def test_dynamic_subscript_deep_in_chain_still_rejected():
    with pytest.raises(FootprintAnalysisError) as exc:
        extract_doc_paths('doc["a"][1 + 2]["b"]')
    assert exc.value.code == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert exc.value.details["index"] == "1 + 2"


def test_non_doc_dynamic_subscripts_are_ignored():
    # 不以 doc 为根的切片/动态下标不影响分析
    assert extract_doc_paths('(1, 2, 3)[0:1] and doc["x"]') == {"/x"}


# ---------- 规则集级聚合 ----------


def _chain_ruleset():
    # a <- b <- c；evaluation_order 即 [a, b, c]
    return compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            _rule("a", 'doc["shared"] >= 1', '{"k": doc["a_only"]}'),
            _rule(
                "b",
                'a.hit and doc["b_only"] == "x"',
                depends_on=["a"],
            ),
            _rule(
                "c",
                'b.hit or doc["shared"]',
                action='doc["c_out"]',
                depends_on=["b"],
            ),
        ],
    )


def test_single_rule_union_of_condition_and_action():
    compiled = _chain_ruleset()
    result = input_footprint(compiled, ["a"])
    assert result["rule_codes"] == ["a"]
    fp = result["footprints"][0]
    assert fp["rule_code"] == "a"
    assert [p["path"] for p in fp["paths"]] == ["/a_only", "/shared"]
    assert fp["paths"][0] == {"path": "/a_only", "used_by": ["a"]}
    assert fp["paths"][1] == {"path": "/shared", "used_by": ["a"]}


def test_upstream_closure_paths_and_used_by_order():
    compiled = _chain_ruleset()
    result = input_footprint(compiled, ["c"])
    paths = {p["path"]: p["used_by"] for p in result["footprints"][0]["paths"]}
    # /shared 被 a 与 c 读取；used_by 按 evaluation_order
    assert paths["/shared"] == ["a", "c"]
    assert paths["/a_only"] == ["a"]
    assert paths["/b_only"] == ["b"]
    assert paths["/c_out"] == ["c"]
    # 路径字典序
    assert [p["path"] for p in result["footprints"][0]["paths"]] == sorted(paths)


def test_closure_rules_not_reading_a_path_are_not_in_used_by():
    compiled = _chain_ruleset()
    result = input_footprint(compiled, ["b"])
    paths = {p["path"]: p["used_by"] for p in result["footprints"][0]["paths"]}
    # b 的闭包是 {a, b}；c 不在其中
    assert set(paths) == {"/a_only", "/b_only", "/shared"}
    assert paths["/shared"] == ["a"]  # b 不读 /shared


def test_multiple_targets_dedup_preserve_order():
    compiled = _chain_ruleset()
    result = input_footprint(compiled, ["b", "b", "a"])
    assert result["rule_codes"] == ["b", "a"]
    assert [f["rule_code"] for f in result["footprints"]] == ["b", "a"]


def test_action_only_rule_reference_is_part_of_upstream():
    # 条件为空、动作里引用 a：编译期不强制动作引用声明依赖，
    # 足迹仍应把 a 视作上游
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            _rule("a", 'doc["up"] == 1'),
            _rule("b", "", action='a.value + doc["own"]'),
        ],
    )
    result = input_footprint(compiled, ["b"])
    paths = {p["path"]: p["used_by"] for p in result["footprints"][0]["paths"]}
    assert paths == {"/up": ["a"], "/own": ["b"]}


def test_unknown_rule_raises_like_impact():
    compiled = _chain_ruleset()
    with pytest.raises(FootprintAnalysisError) as exc:
        input_footprint(compiled, ["a", "nope"])
    assert exc.value.code == "RULE_NOT_FOUND"
    assert exc.value.details == {"rule_codes": ["nope"]}


def test_dynamic_access_in_upstream_rule_rejects_whole_analysis():
    # 目标 b 本身只有静态访问，但其上游 a 含动态下标 -> 整体 422，无部分结果
    compiled = compile_ruleset(
        "t",
        "",
        "collect_all",
        [
            _rule("a", 'doc["k" + "x"] == 1'),
            _rule("b", "a.hit", '{"ok": true}', ["a"]),
        ],
    )
    with pytest.raises(FootprintAnalysisError) as exc:
        input_footprint(compiled, ["b"])
    assert exc.value.code == "UNSUPPORTED_DOCUMENT_ACCESS"
    assert exc.value.details["rule_code"] == "a"
    assert exc.value.details["part"] == "condition"


def test_demo_ruleset_footprints():
    from app.seed import DEMO_PAYLOAD

    compiled = compile_ruleset(
        DEMO_PAYLOAD["name"],
        DEMO_PAYLOAD["description"],
        DEMO_PAYLOAD["default_conflict_policy"],
        DEMO_PAYLOAD["rules"],
    )

    vip = input_footprint(compiled, ["is_vip"])["footprints"][0]
    assert vip["paths"] == [
        {"path": "/customer/vip", "used_by": ["is_vip"]},
    ]

    disc = input_footprint(compiled, ["vip_discount"])["footprints"][0]
    assert [p["path"] for p in disc["paths"]] == ["/amount", "/customer/vip"]
    users = {p["path"]: p["used_by"] for p in disc["paths"]}
    assert users["/amount"] == ["vip_discount"]
    assert users["/customer/vip"] == ["is_vip"]

    review = input_footprint(compiled, ["manual_review"])["footprints"][0]
    users = {p["path"]: p["used_by"] for p in review["paths"]}
    assert users == {
        "/amount": ["vip_discount", "manual_review"],
        "/customer/vip": ["is_vip"],
    }

    shipping = input_footprint(compiled, ["free_shipping"])["footprints"][0]
    users = {p["path"]: p["used_by"] for p in shipping["paths"]}
    assert users == {"/amount": ["large_order", "free_shipping"], "/region": ["free_shipping"]}

    coupon = input_footprint(compiled, ["coupon_save10"])["footprints"][0]
    assert coupon["paths"] == [{"path": "/coupons", "used_by": ["coupon_save10"]}]
