"""规则静态影响分析：反向传递依赖闭包 + 冲突组联动。

仅做静态分析，**不执行任何文档求值、不产生决策结果**：

1. 基于编译产物中各规则的 ``depends_on`` 与条件表达式引用
   （保存期已强制表达式引用必须显式声明依赖，两者并集即完整出边）
   构建反向依赖图 ``dependency -> [dependents]``；
2. 从输入规则出发 BFS 求反向传递依赖闭包，BFS 层数即“受影响层级”
   （0 为输入规则本身，1 为直接依赖方 direct dependents，≥2 为传递依赖方）；
3. 与任一受影响规则（含输入）处于同一 ``conflict_group`` 的其余规则
   不会经依赖边被波及，但运行时可能因冲突策略联动（被压制或反向胜出），
   标记为 ``coupled_rules``。联动关系不继续向外传播。
"""

from __future__ import annotations

from .compiler import CompiledRuleset


class ImpactAnalysisError(ValueError):
    """影响分析无法完成（如规则编码在该版本不存在）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def reverse_dependencies(compiled: CompiledRuleset) -> dict[str, list[str]]:
    """构建反向依赖图：被依赖规则编码 -> 依赖它的规则编码列表。

    出边取 ``depends_on`` 与条件表达式引用的并集（编译期已保证引用必在
    ``depends_on`` 中，并集是防御性写法）。依赖方按 evaluation_order 出现，
    因此每个列表天然按拓扑序排列且无重复。
    """
    reverse: dict[str, list[str]] = {code: [] for code in compiled.by_code}
    for code in compiled.evaluation_order:
        rule = compiled.by_code[code]
        deps = set(rule.depends_on) | set(rule.referenced_rules)
        for dep in deps:
            if dep in reverse:
                reverse[dep].append(code)
    return reverse


def impact_analysis(
    compiled: CompiledRuleset, rule_codes: list[str]
) -> dict[str, object]:
    """计算给定规则在该编译版本上的静态影响闭包。

    返回结构：

    - ``rule_codes``：去重保序后的输入规则回显；
    - ``affected``：输入规则（level=0）与全部受影响规则，按
      evaluation_order 排列，每项带层级与分类 relation；
    - ``direct_dependents`` / ``transitive_dependents``：按
      evaluation_order 排列的规则编码列表（互斥，且不含输入规则）；
    - ``coupled_rules``：同冲突组联动项，按 evaluation_order 排列。
    """
    # 去重保序
    inputs = list(dict.fromkeys(rule_codes))
    missing = [code for code in inputs if code not in compiled.by_code]
    if missing:
        raise ImpactAnalysisError(
            "RULE_NOT_FOUND",
            f"规则编码在该版本中不存在：{missing}",
            {"rule_codes": missing},
        )

    reverse = reverse_dependencies(compiled)

    # ---------- BFS 反向传递闭包 + 层级 ----------
    levels: dict[str, int] = {code: 0 for code in inputs}
    frontier = list(inputs)
    depth = 0
    while frontier:
        depth += 1
        next_frontier: list[str] = []
        for code in frontier:
            for dependent in reverse[code]:
                if dependent not in levels:
                    levels[dependent] = depth
                    next_frontier.append(dependent)
        frontier = next_frontier

    input_set = set(inputs)

    # 直接依赖方：任一输入规则的反向邻居（再剔除输入本身）
    direct: set[str] = set()
    for code in inputs:
        direct.update(reverse[code])
    direct -= input_set
    transitive = set(levels) - input_set - direct

    # ---------- 冲突组成员表（按 evaluation_order） ----------
    group_members: dict[str, list[str]] = {}
    for code in compiled.evaluation_order:
        group = compiled.by_code[code].conflict_group
        if group:
            group_members.setdefault(group, []).append(code)

    # 与闭包内任一规则同组、但自身不在闭包中的规则为联动项；
    # coupled_with 取 evaluation_order 上第一个触发联动的受影响规则。
    coupled: dict[str, tuple[str, str]] = {}
    for code in compiled.evaluation_order:
        if code not in levels:
            continue
        group = compiled.by_code[code].conflict_group
        if not group:
            continue
        for peer in group_members[group]:
            if peer in levels:
                # 已作为输入/依赖项命中分类，依赖关系优先，不再重复标记
                continue
            coupled.setdefault(peer, (group, code))

    # ---------- 按 evaluation_order 组装输出 ----------
    affected: list[dict[str, object]] = []
    for code in compiled.evaluation_order:
        if code not in levels:
            continue
        if code in input_set:
            relation = "input"
        elif code in direct:
            relation = "direct_dependent"
        else:
            relation = "transitive_dependent"
        affected.append(
            {"rule_code": code, "level": levels[code], "relation": relation}
        )

    return {
        "rule_codes": inputs,
        "affected": affected,
        "direct_dependents": [
            code for code in compiled.evaluation_order if code in direct
        ],
        "transitive_dependents": [
            code for code in compiled.evaluation_order if code in transitive
        ],
        "coupled_rules": [
            {
                "rule_code": code,
                "conflict_group": coupled[code][0],
                "coupled_with": coupled[code][1],
                "level": None,
            }
            for code in compiled.evaluation_order
            if code in coupled
        ],
    }
