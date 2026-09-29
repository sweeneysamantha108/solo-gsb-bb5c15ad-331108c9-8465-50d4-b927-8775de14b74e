"""首次启动时写入演示规则集 demo-promotions。

由环境变量 SEED_DEMO=1 开启（Docker 默认开启）。
已存在规则集（或该 demo id 已存在）时不重复写入。
"""

from __future__ import annotations

import sqlite3

from . import crud

DEMO_ID = "demo-promotions"

DEMO_PAYLOAD = {
    "name": "促销规则演示",
    "description": (
        "VIP、大额订单、优惠券、包邮、人工审核示例。"
        "discount 组使用 priority，coupon 组使用 first_hit，其余默认 collect_all。"
    ),
    "default_conflict_policy": "collect_all",
    "rules": [
        {
            "code": "is_vip",
            "name": "VIP 客户",
            "priority": 0,
            "condition": 'doc["customer"]["vip"] == true',
            "action": '{"vip": true}',
            "depends_on": [],
        },
        {
            "code": "large_order",
            "name": "大额订单",
            "priority": 10,
            "conflict_group": "discount",
            "conflict_policy": "priority",
            "condition": 'doc["amount"] >= 1000',
            "action": '{"discount": 0.1, "reason": "large_order"}',
            "depends_on": [],
        },
        {
            "code": "vip_discount",
            "name": "VIP 专属折扣",
            "priority": 20,
            "conflict_group": "discount",
            "conflict_policy": "priority",
            "condition": 'is_vip.hit and doc["amount"] >= 500',
            "action": '{"discount": 0.15, "reason": "vip_discount"}',
            "depends_on": ["is_vip"],
        },
        {
            "code": "free_shipping",
            "name": "北方地区包邮",
            "condition": '(large_order.hit or doc["amount"] >= 100) and doc["region"] == "north"',
            "action": '{"free_shipping": true}',
            "depends_on": ["large_order"],
        },
        {
            "code": "coupon_save10",
            "name": "优惠券 SAVE10",
            "conflict_group": "coupon",
            "conflict_policy": "first_hit",
            "condition": 'contains(doc["coupons"], "SAVE10")',
            "action": '{"coupon": "SAVE10"}',
            "depends_on": [],
        },
        {
            "code": "coupon_save20",
            "name": "优惠券 SAVE20",
            "conflict_group": "coupon",
            "conflict_policy": "first_hit",
            "condition": 'contains(doc["coupons"], "SAVE20")',
            "action": '{"coupon": "SAVE20"}',
            "depends_on": [],
        },
        {
            "code": "manual_review",
            "name": "非 VIP 巨额订单人工审核",
            "condition": 'vip_discount.hit == false and doc["amount"] >= 3000',
            "action": '{"manual_review": true}',
            "depends_on": ["vip_discount"],
        },
    ],
}


def seed_demo(conn: sqlite3.Connection) -> bool:
    """写入演示规则集；已存在则跳过。返回是否执行了写入。"""
    if crud.get_ruleset(conn, DEMO_ID) is not None:
        return False
    payload = {"id": DEMO_ID, **DEMO_PAYLOAD}
    crud.create_ruleset(conn, payload)
    return True
