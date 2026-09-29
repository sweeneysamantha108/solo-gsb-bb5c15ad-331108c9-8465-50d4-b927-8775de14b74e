"""跨版本预演的差异归并：决策 JSON 路径差异 + 命中规则/冲突摘要。

决策值是任意 JSON，差异按 **RFC 6901 JSON Pointer 路径**归并：
- 双方同路径且同类型的容器（dict / list）递归比较；
- 只在一侧出现的子树逐叶展开（added / removed），叶子携带旧值/新值；
- 同路径类型不同（dict 与 list、bool 与 int 等）记为 changed；
- 列表按下标对齐比较，多出的元素记为 added / removed。
"""

from __future__ import annotations

from typing import Any


# ---------- JSON 路径差异 ----------


def _escape(token: Any) -> str:
    """RFC 6901 转义：~ -> ~0、/ -> ~1（顺序不能颠倒）。"""
    return str(token).replace("~", "~0").replace("/", "~1")


def _join(path: str, token: Any) -> str:
    return f"{path}/{_escape(token)}"


def _equal(base: Any, target: Any) -> bool:
    """JSON 语义下的深度相等；bool 与 int 不互相等同（True != 1）。"""
    if isinstance(base, (list, tuple)):
        if not isinstance(target, (list, tuple)) or len(base) != len(target):
            return False
        return all(_equal(a, b) for a, b in zip(base, target))
    if isinstance(base, dict):
        if not isinstance(target, dict) or set(base) != set(target):
            return False
        return all(k in target and _equal(base[k], target[k]) for k in base)
    # 容器与标量、bool 与 int/float、str 与 number 等类型不一致直接判不等
    if type(base) is not type(target):
        return False
    return base == target


def _add_only(value: Any, path: str, out: list[dict[str, Any]], key: str) -> None:
    """展开只在一侧出现的子树：dict 按键、list 按下标逐叶登记；空容器占自身路径。

    条目恒含 old/new 两键，缺席一侧为 None。
    """
    if isinstance(value, dict):
        if not value:
            out.append(_leaf_entry(path, key, {}))
            return
        for k, v in value.items():
            _add_only(v, _join(path, k), out, key)
    elif isinstance(value, (list, tuple)):
        if not value:
            out.append(_leaf_entry(path, key, []))
            return
        for i, v in enumerate(value):
            _add_only(v, f"{path}/{i}", out, key)
    else:
        out.append(_leaf_entry(path, key, value))


def _leaf_entry(path: str, key: str, value: Any) -> dict[str, Any]:
    if key == "new":
        return {"path": path, "old": None, "new": value}
    return {"path": path, "old": value, "new": None}


def _diff(
    base: Any,
    target: Any,
    path: str,
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    changed: list[dict[str, Any]],
) -> None:
    if isinstance(base, dict) and isinstance(target, dict):
        base_keys = set(base)
        target_keys = set(target)
        for key in target_keys - base_keys:
            _add_only(target[key], _join(path, key), added, "new")
        for key in base_keys - target_keys:
            _add_only(base[key], _join(path, key), removed, "old")
        for key in base_keys & target_keys:
            _diff(base[key], target[key], _join(path, key), added, removed, changed)
        return

    if isinstance(base, (list, tuple)) and isinstance(target, (list, tuple)):
        common = min(len(base), len(target))
        for i in range(common):
            _diff(base[i], target[i], f"{path}/{i}", added, removed, changed)
        for i in range(common, len(target)):
            _add_only(target[i], f"{path}/{i}", added, "new")
        for i in range(common, len(base)):
            _add_only(base[i], f"{path}/{i}", removed, "old")
        return

    # 同路径类型不一致（含 dict↔list）或标量值不同
    if not _equal(base, target):
        changed.append({"path": path, "old": base, "new": target})


def decision_diff(
    base_decision: Any, target_decision: Any
) -> dict[str, list[dict[str, Any]]]:
    """归并两版决策值的差异，返回按路径排序的 added/removed/changed。"""
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    _diff(base_decision, target_decision, "", added, removed, changed)
    added.sort(key=lambda item: item["path"])
    removed.sort(key=lambda item: item["path"])
    changed.sort(key=lambda item: item["path"])
    return {"added": added, "removed": removed, "changed": changed}


# ---------- 命中规则 / 冲突摘要 ----------


def list_diff(base_codes: list[str], target_codes: list[str]) -> dict[str, list[str]]:
    """有序编码列表的两侧回显与新增/移除（不考虑重复编码）。"""
    base_set = set(base_codes)
    target_set = set(target_codes)
    return {
        "base": list(base_codes),
        "target": list(target_codes),
        "added": [code for code in target_codes if code not in base_set],
        "removed": [code for code in base_codes if code not in target_set],
    }


def _is_field_conflict(conflict: dict[str, Any]) -> bool:
    return "field" in conflict


def _group_signature(conflict: dict[str, Any]) -> tuple:
    return (
        conflict.get("policy"),
        conflict.get("winner"),
        tuple(
            sorted(s.get("rule_code") for s in conflict.get("suppressed", []))
        ),
    )


def _field_signature(conflict: dict[str, Any]) -> tuple:
    return (conflict.get("previous_winner"), conflict.get("winner"))


def conflict_diff(
    base_conflicts: list[dict[str, Any]],
    target_conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    """冲突摘要：整组冲突按 group、collect_all 字段覆盖按 field 归并比较。"""
    base_groups = {
        c["group"]: c for c in base_conflicts if not _is_field_conflict(c)
    }
    target_groups = {
        c["group"]: c for c in target_conflicts if not _is_field_conflict(c)
    }
    base_fields = {
        c["field"]: c for c in base_conflicts if _is_field_conflict(c)
    }
    target_fields = {
        c["field"]: c for c in target_conflicts if _is_field_conflict(c)
    }

    added = [target_groups[k] for k in sorted(target_groups.keys() - base_groups.keys())]
    removed = [base_groups[k] for k in sorted(base_groups.keys() - target_groups.keys())]
    changed: list[dict[str, Any]] = []

    for group in sorted(base_groups.keys() & target_groups.keys()):
        old, new = base_groups[group], target_groups[group]
        if _group_signature(old) != _group_signature(new):
            changed.append({"group": group, "field": None, "old": old, "new": new})

    # 字段级覆盖说明追加在整组冲突之后
    added.extend(
        target_fields[k] for k in sorted(target_fields.keys() - base_fields.keys())
    )
    removed.extend(
        base_fields[k] for k in sorted(base_fields.keys() - target_fields.keys())
    )
    for field in sorted(base_fields.keys() & target_fields.keys()):
        old, new = base_fields[field], target_fields[field]
        if _field_signature(old) != _field_signature(new):
            changed.append({"group": None, "field": field, "old": old, "new": new})

    return {
        "base": list(base_conflicts),
        "target": list(target_conflicts),
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def compare_results(
    base_result: dict[str, Any], target_result: dict[str, Any]
) -> dict[str, Any]:
    """两版求值结果的命中规则与冲突摘要（决策差异由 decision_diff 单独计算）。"""
    return {
        "fired_rules": list_diff(
            base_result["fired_rule_codes"], target_result["fired_rule_codes"]
        ),
        "hit_rules": list_diff(
            base_result["hit_rule_codes"], target_result["hit_rule_codes"]
        ),
        "conflicts": conflict_diff(
            base_result.get("conflicts", []), target_result.get("conflicts", [])
        ),
    }
