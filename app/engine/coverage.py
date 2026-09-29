"""多文档覆盖分析：逐项不落库求值，汇总规则命中与冲突场景，贪心选取代表输入。

覆盖单元（coverage unit）是一次求值中可观察到的最小覆盖证据，分三类：

- ``rule``：某条规则在某份文档上命中（fired）。身份为
  ``("rule", rule_code)``；
- ``group``：priority / first_hit 冲突组的一次整组裁决。身份为
  ``("group", group, winner, suppressed_rule_codes)``，被压制规则按编码排序，
  因此“同组同胜出方但被压制集合不同”是不同场景；
- ``field``：collect_all 下同名字段被后命中规则覆盖。身份为
  ``("field", field, previous_winner, winner)``。

每份**成功**结果的覆盖单元 = ``fired_rule_codes`` 产生的 rule 单元 +
``conflicts`` 中每条解释产生的 group/field 单元（冲突涉及的规则同时以 rule
单元计入）。失败项不产生任何单元，也不参与代表选择。

代表输入按确定性贪心集合覆盖选出：每轮选择“相对尚未覆盖集合新增单元最多”
的成功项，并列时取最小输入下标；无新增即停止。
"""

from __future__ import annotations

from typing import Any

# 覆盖单元身份元组：
# ("rule", code)
# ("group", group, winner, (suppressed_code, ...))
# ("field", field, previous_winner, winner)
CoverageUnit = tuple

RULE = "rule"
GROUP = "group"
FIELD = "field"


def _suppressed_codes(conflict: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        sorted(
            s.get("rule_code")
            for s in conflict.get("suppressed", [])
            if s.get("rule_code")
        )
    )


def _is_field_conflict(conflict: dict[str, Any]) -> bool:
    return "field" in conflict


def conflict_identity(conflict: dict[str, Any]) -> CoverageUnit:
    """单条冲突解释的身份（group/field 覆盖单元）。"""
    if _is_field_conflict(conflict):
        return (
            FIELD,
            conflict["field"],
            conflict.get("previous_winner"),
            conflict.get("winner"),
        )
    return (
        GROUP,
        conflict.get("group"),
        conflict.get("winner"),
        _suppressed_codes(conflict),
    )


def rule_identity(code: str) -> CoverageUnit:
    return (RULE, code)


def extract_units(result: dict[str, Any]) -> set[CoverageUnit]:
    """一份成功求值结果的全部覆盖单元。"""
    units: set[CoverageUnit] = {rule_identity(code) for code in result["fired_rule_codes"]}
    for conflict in result.get("conflicts", []):
        identity = conflict_identity(conflict)
        units.add(identity)
        # 冲突涉及的规则（胜出方 / 被压制方 / 字段的前后写入方）同时以 rule 单元计入
        if identity[0] == GROUP:
            if identity[2]:
                units.add(rule_identity(identity[2]))
            units.update(rule_identity(code) for code in identity[3])
        else:
            if identity[2]:
                units.add(rule_identity(identity[2]))
            if identity[3]:
                units.add(rule_identity(identity[3]))
    return units


def unit_to_dict(unit: CoverageUnit) -> dict[str, Any]:
    kind = unit[0]
    if kind == RULE:
        return {"kind": RULE, "rule_code": unit[1]}
    if kind == GROUP:
        return {
            "kind": GROUP,
            "group": unit[1],
            "winner": unit[2],
            "suppressed_rule_codes": list(unit[3]),
        }
    return {
        "kind": FIELD,
        "field": unit[1],
        "previous_winner": unit[2],
        "winner": unit[3],
        "suppressed_rule_codes": [],
    }


def _unit_sort_key(unit: CoverageUnit) -> tuple:
    """覆盖单元输出的确定性排序键。"""
    kind = unit[0]
    if kind == RULE:
        return (0, unit[1], "", "", ())
    if kind == GROUP:
        return (1, unit[1] or "", unit[2] or "", "", unit[3])
    return (2, unit[1] or "", unit[3] or "", unit[2] or "", ())


def _units_out(units: set[CoverageUnit]) -> list[dict[str, Any]]:
    return [unit_to_dict(u) for u in sorted(units, key=_unit_sort_key)]


def _scenario_from_conflict(
    conflict: dict[str, Any], observed_at_indexes: list[int]
) -> dict[str, Any]:
    """由首见冲突解释与观察下标列表构造冲突场景输出。"""
    if _is_field_conflict(conflict):
        previous_winner = conflict.get("previous_winner")
        winner = conflict.get("winner")
        units = {conflict_identity(conflict)}
        for code in (previous_winner, winner):
            if code:
                units.add(rule_identity(code))
        return {
            "kind": FIELD,
            "group": None,
            "field": conflict["field"],
            "policy": conflict.get("policy"),
            "winner": winner,
            "suppressed_rule_codes": [],
            "previous_winner": previous_winner,
            "reason": conflict.get("reason"),
            "observed_count": len(observed_at_indexes),
            "observed_at_indexes": list(observed_at_indexes),
            "coverage_units": _units_out(units),
        }

    winner = conflict.get("winner")
    suppressed = list(_suppressed_codes(conflict))
    units = {conflict_identity(conflict)}
    if winner:
        units.add(rule_identity(winner))
    units.update(rule_identity(code) for code in suppressed)
    return {
        "kind": GROUP,
        "group": conflict.get("group"),
        "field": None,
        "policy": conflict.get("policy"),
        "winner": winner,
        "suppressed_rule_codes": suppressed,
        "previous_winner": None,
        "reason": None,
        "observed_count": len(observed_at_indexes),
        "observed_at_indexes": list(observed_at_indexes),
        "coverage_units": _units_out(units),
    }


def _select_representatives(
    units_by_index: dict[int, set[CoverageUnit]],
) -> list[int]:
    """确定性贪心集合覆盖：每轮新增单元最多者入选，并列取最小下标，直至无新增。"""
    remaining: set[CoverageUnit] = set()
    for units in units_by_index.values():
        remaining |= units

    available = set(units_by_index)
    chosen: list[int] = []
    while available:
        best = min(
            available,
            key=lambda index: (-len(units_by_index[index] & remaining), index),
        )
        gained = units_by_index[best] & remaining
        if not gained:
            break
        chosen.append(best)
        remaining -= gained
        available.discard(best)
    return chosen


def build_coverage(
    evaluation_order: list[str],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    """汇总逐份求值结果，构造覆盖统计、冲突场景与代表输入下标。

    ``outcomes`` 每项形如
    ``{"index", "status", "result", "error"}``，须按输入顺序给出；
    仅 ``status == "success"`` 的项参与统计与选择。
    """
    # 规则命中统计（成功文档中 fired 即计一次）
    hit_indexes: dict[str, list[int]] = {code: [] for code in evaluation_order}
    # 冲突身份 -> 首见冲突解释与观察下标（注册顺序即首次观察顺序，保证确定性）
    first_seen: dict[CoverageUnit, dict[str, Any]] = {}
    observed: dict[CoverageUnit, list[int]] = {}

    items: list[dict[str, Any]] = []
    units_by_index: dict[int, set[CoverageUnit]] = {}
    success_count = 0
    error_count = 0

    for outcome in outcomes:
        index = outcome["index"]
        if outcome["status"] != "success":
            error_count += 1
            items.append(
                {
                    "index": index,
                    "status": "error",
                    "fired_rule_codes": [],
                    "conflicts": [],
                    "coverage_units": [],
                    "error": outcome["error"],
                }
            )
            continue

        success_count += 1
        result = outcome["result"]
        units = extract_units(result)
        units_by_index[index] = units

        for code in result["fired_rule_codes"]:
            hit_indexes.setdefault(code, []).append(index)

        for conflict in result.get("conflicts", []):
            identity = conflict_identity(conflict)
            if identity not in observed:
                observed[identity] = []
                first_seen[identity] = conflict
            observed[identity].append(index)

        items.append(
            {
                "index": index,
                "status": "success",
                "fired_rule_codes": list(result["fired_rule_codes"]),
                "conflicts": list(result.get("conflicts", [])),
                "coverage_units": _units_out(units),
                "error": None,
            }
        )

    rule_coverage = [
        {
            "code": code,
            "hit_count": len(hit_indexes.get(code, ())),
            "covered_by_indexes": sorted(hit_indexes.get(code, ())),
        }
        for code in evaluation_order
    ]
    # 版本快照中存在、但所有成功文档都未命中的规则（按评估顺序排列）
    uncovered = [
        code
        for code in evaluation_order
        if not hit_indexes.get(code)
    ]

    conflict_scenarios = [
        _scenario_from_conflict(first_seen[identity], observed[identity])
        for identity in observed
    ]

    representative_indexes = _select_representatives(units_by_index)

    return {
        "total": len(outcomes),
        "success_count": success_count,
        "error_count": error_count,
        "rule_coverage": rule_coverage,
        "uncovered_rule_codes": uncovered,
        "conflict_scenarios": conflict_scenarios,
        "representative_indexes": representative_indexes,
        "items": items,
    }
