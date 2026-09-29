"""规则求值器：依赖图上的定点迭代 + 冲突处理。

算法：
1. 按 depends_on 做拓扑排序（保存期已保证无环），第一轮所有规则均“未命中”；
2. 每一轮按拓扑序评估规则。由于条件引用的规则必然是其依赖、且依赖在拓扑序
   中位于前方，评估时直接采用同轮已算出的最新结果（Gauss-Seidel 更新），
   因此一轮即可得到与依赖深度无关的精确结果；
3. 再来一轮，若所有规则的命中状态与动作值均不再变化，即达到定点（稳定结果）；
   理论上 DAG 上两轮内必然收敛，MAX_ITERATIONS 仅为防御性兜底；
4. 在命中集合上按冲突组执行策略，得到最终决策与冲突解释。
"""

from __future__ import annotations

from typing import Any

from .ast_eval import EvaluationError, RuleRef, safe_eval
from .compiler import CompiledRule, CompiledRuleset

MAX_ITERATIONS = 100


class EvaluationFailed(ValueError):
    """求值无法完成（不收敛等）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _evaluate_rule(
    rule: CompiledRule,
    document: Any,
    state: dict[str, tuple[bool, Any]],
) -> tuple[bool, Any]:
    namespace: dict[str, Any] = {"doc": document}
    for code, (hit, value) in state.items():
        namespace[code] = RuleRef(hit, value)

    if not rule.condition:
        cond = True
    else:
        try:
            cond = bool(safe_eval(rule.condition, namespace))
        except EvaluationError:
            # 缺字段/类型不匹配视为条件不成立，轨迹中记录错误
            raise
    if not cond:
        return False, None

    if not rule.action:
        return True, None
    value = safe_eval(rule.action, namespace)
    return True, value


def evaluate(compiled: CompiledRuleset, document: Any) -> dict[str, Any]:
    order = compiled.evaluation_order

    # state: code -> (hit, value)。首轮全部“未命中”。
    # 依赖图是 DAG 且条件中引用的规则必须在 depends_on 中，
    # 因此拓扑序遍历时被依赖规则一定先算完——同轮内直接使用最新值
    # （Gauss-Seidel），一轮即可得到精确结果，下一轮确认稳定。
    state: dict[str, tuple[bool, Any]] = {code: (False, None) for code in order}

    iterations = 0

    for iterations in range(1, MAX_ITERATIONS + 1):
        before = dict(state)
        rule_errors: dict[str, str] = {}
        for code in order:
            rule = compiled.by_code[code]
            try:
                state[code] = _evaluate_rule(rule, document, state)
            except EvaluationError as exc:
                state[code] = (False, None)
                rule_errors[code] = str(exc)

        if state == before:
            break
    else:
        raise EvaluationFailed(
            "NOT_CONVERGED",
            f"规则评估在 {MAX_ITERATIONS} 轮内未达到稳定状态",
            {"max_iterations": MAX_ITERATIONS},
        )

    traces = [
        {
            "rule_code": code,
            "hit": state[code][0],
            "value": state[code][1],
            **({"error": rule_errors[code]} if code in rule_errors else {}),
        }
        for code in order
    ]

    fired_codes = [code for code in order if state[code][0]]

    # ---------- 冲突处理 ----------
    groups: dict[str, list[str]] = {}
    ungrouped: list[str] = []
    for code in fired_codes:
        group = compiled.by_code[code].conflict_group
        if group:
            groups.setdefault(group, []).append(code)
        else:
            ungrouped.append(code)

    survivors: list[str] = list(ungrouped)
    conflicts: list[dict[str, Any]] = []

    for group, members in groups.items():
        policy = compiled.effective_policy(compiled.by_code[members[0]])
        winner, suppressed, detail = _resolve_group(compiled, members, policy)
        if policy == "collect_all":
            survivors.extend(members)
            conflicts.extend(detail)  # 仅字段级覆盖说明（可能为空）
        else:
            survivors.append(winner)
            conflicts.append(
                {
                    "group": group,
                    "policy": policy,
                    "winner": winner,
                    "suppressed": suppressed,
                }
            )

    # survivors 按拓扑序输出，即命中的规则生效顺序
    survivors.sort(key=order.index)
    decision, field_conflicts = _build_decision(compiled, survivors, state)
    conflicts.extend(field_conflicts)

    return {
        "fired_rule_codes": fired_codes,
        "hit_rule_codes": survivors,
        "evaluation_order": order,
        "iterations": iterations,
        "converged": True,
        "decision": decision,
        "conflicts": conflicts,
        "traces": traces,
    }


def _resolve_group(
    compiled: CompiledRuleset, members: list[str], policy: str
) -> tuple[str | None, list[dict], list[dict]]:
    """members 已按拓扑序排列。返回 (winner, suppressed[], collect_all字段冲突[])。"""
    if policy == "priority":
        winner = max(
            members,
            key=lambda c: (compiled.by_code[c].priority, -compiled.by_code[c].order_index),
        )
        suppressed = [
            {
                "rule_code": c,
                "reason": "lower_priority",
                "priority": compiled.by_code[c].priority,
            }
            for c in members
            if c != winner
        ]
        return winner, suppressed, []

    if policy == "first_hit":
        winner = members[0]
        suppressed = [
            {
                "rule_code": c,
                "reason": "not_first_hit",
                "evaluation_index": compiled.by_code[c].order_index,
            }
            for c in members[1:]
        ]
        return winner, suppressed, []

    return None, [], []


def _build_decision(
    compiled: CompiledRuleset,
    survivors: list[str],
    state: dict[str, tuple[bool, Any]],
) -> tuple[dict[str, Any], list[dict]]:
    decision: dict[str, Any] = {}
    loose: list[Any] = []
    field_conflicts: list[dict] = []
    # 记录每个字段当前由哪条规则写入，便于给出覆盖解释
    field_owner: dict[str, str] = {}

    for code in survivors:
        # 未配置 action 的规则只用于参与命中判断/供其他规则引用，不产出决策值
        if not compiled.by_code[code].action:
            continue
        value = state[code][1]
        if value is None:
            continue
        if isinstance(value, dict):
            for key, val in value.items():
                if key in field_owner:
                    field_conflicts.append(
                        {
                            "group": None,
                            "policy": "collect_all",
                            "field": key,
                            "previous_winner": field_owner[key],
                            "winner": code,
                            "reason": "field_overwritten",
                        }
                    )
                decision[key] = val
                field_owner[key] = code
        else:
            loose.append({"rule_code": code, "value": value})

    if loose:
        decision["results"] = loose
    return decision, field_conflicts
