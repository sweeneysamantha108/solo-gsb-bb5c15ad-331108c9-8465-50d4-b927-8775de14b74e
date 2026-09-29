"""规则集编译器。

负责保存前的全部静态校验：
- 规则编码合法性 / 唯一性
- 条件、动作表达式可编译
- depends_on 指向存在的规则
- 条件中引用的规则必须显式声明依赖（UNDECLARED_REFERENCE）
- 依赖图无循环（CYCLIC_DEPENDENCY，返回环路径）
- 同一冲突组内的显式 conflict_policy 必须一致
- 冲突组名与规则编码不冲突（规则引用仅支持 .hit/.value）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ast_eval import CompileError, parse, referenced_names

CODE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

POLICIES = ("collect_all", "priority", "first_hit")

# 表达式中的保留名字，不算规则引用
RESERVED_NAMES = {"doc", "true", "false", "null", "True", "False", "None"}


class RulesetCompileError(ValueError):
    """规则集编译失败。code 为机器可读错误码。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class CompiledRule:
    code: str
    name: str
    description: str
    priority: int
    conflict_group: str | None
    conflict_policy: str | None  # 可能为 None（继承规则集默认）
    depends_on: tuple[str, ...]
    condition: str
    action: str
    referenced_rules: frozenset[str]
    order_index: int  # 在提交列表中的位置，用于稳定排序


@dataclass
class CompiledRuleset:
    name: str
    description: str
    default_conflict_policy: str
    rules: list[CompiledRule] = field(default_factory=list)
    by_code: dict[str, CompiledRule] = field(default_factory=dict)
    # 拓扑评估顺序
    evaluation_order: list[str] = field(default_factory=list)

    def effective_policy(self, rule: CompiledRule) -> str:
        return rule.conflict_policy or self.default_conflict_policy


def compile_ruleset(
    name: str,
    description: str,
    default_conflict_policy: str,
    raw_rules: list[dict],
) -> CompiledRuleset:
    if default_conflict_policy not in POLICIES:
        raise RulesetCompileError(
            "INVALID_POLICY",
            f"默认冲突策略必须是 {POLICIES} 之一，收到：{default_conflict_policy!r}",
        )
    if not raw_rules:
        raise RulesetCompileError(
            "EMPTY_RULESET", "规则集至少需要包含一条规则"
        )

    compiled = CompiledRuleset(
        name=name,
        description=description or "",
        default_conflict_policy=default_conflict_policy,
    )

    seen_codes: set[str] = set()
    for index, raw in enumerate(raw_rules):
        rule = _compile_rule(raw, index)
        if rule.code in seen_codes:
            raise RulesetCompileError(
                "DUPLICATE_RULE_CODE",
                f"规则编码重复：{rule.code}",
                {"rule_code": rule.code},
            )
        seen_codes.add(rule.code)
        compiled.rules.append(rule)
        compiled.by_code[rule.code] = rule

    # 依赖指向存在的规则
    for rule in compiled.rules:
        for dep in rule.depends_on:
            if dep not in compiled.by_code:
                raise RulesetCompileError(
                    "UNKNOWN_DEPENDENCY",
                    f"规则 {rule.code} 依赖了不存在的规则：{dep}",
                    {"rule_code": rule.code, "dependency": dep},
                )
            if dep == rule.code:
                raise RulesetCompileError(
                    "SELF_DEPENDENCY",
                    f"规则 {rule.code} 不能依赖自身",
                    {"rule_code": rule.code},
                )

    # 条件中引用的规则必须显式声明
    for rule in compiled.rules:
        undeclared = sorted(
            ref
            for ref in rule.referenced_rules
            if ref not in set(rule.depends_on)
        )
        if undeclared:
            raise RulesetCompileError(
                "UNDECLARED_REFERENCE",
                f"规则 {rule.code} 引用了 {undeclared} 但未在 depends_on 中声明",
                {"rule_code": rule.code, "references": undeclared},
            )

    # 冲突组策略一致性
    group_policies: dict[str, tuple[str, str]] = {}
    for rule in compiled.rules:
        if rule.conflict_group and rule.conflict_policy is not None:
            existing = group_policies.get(rule.conflict_group)
            if existing is not None and existing[0] != rule.conflict_policy:
                raise RulesetCompileError(
                    "CONFLICTING_GROUP_POLICY",
                    (
                        f"冲突组 {rule.conflict_group!r} 声明了不同策略："
                        f"{existing[0]}(规则 {existing[1]}) 与 "
                        f"{rule.conflict_policy}(规则 {rule.code})"
                    ),
                    {
                        "group": rule.conflict_group,
                        "policies": [existing[0], rule.conflict_policy],
                    },
                )
            group_policies.setdefault(
                rule.conflict_group, (rule.conflict_policy, rule.code)
            )

    compiled.evaluation_order = topo_order(compiled)
    return compiled


def _compile_rule(raw: dict, index: int) -> CompiledRule:
    code = raw.get("code")
    if not isinstance(code, str) or not CODE_PATTERN.match(code):
        raise RulesetCompileError(
            "INVALID_RULE_CODE",
            f"规则编码非法：{code!r}，需匹配 {CODE_PATTERN.pattern}",
        )

    condition = raw.get("condition") or ""
    action = raw.get("action")
    action_expr = "" if action is None else str(action)

    try:
        cond_names = referenced_names(condition) if condition else set()
        if action_expr:
            parse(action_expr)  # 动作表达式同样需要通过白名单校验
    except CompileError as exc:
        raise RulesetCompileError(
            "INVALID_EXPRESSION",
            f"规则 {code} 的表达式无效：{exc}",
            {"rule_code": code},
        ) from exc

    referenced = frozenset(n for n in cond_names if n not in RESERVED_NAMES)

    deps = raw.get("depends_on") or []
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        raise RulesetCompileError(
            "INVALID_DEPENDENCY",
            f"规则 {code} 的 depends_on 必须是字符串列表",
            {"rule_code": code},
        )

    policy = raw.get("conflict_policy")
    if policy is not None and policy not in POLICIES:
        raise RulesetCompileError(
            "INVALID_POLICY",
            f"规则 {code} 的冲突策略非法：{policy!r}",
            {"rule_code": code},
        )

    priority = raw.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise RulesetCompileError(
            "INVALID_PRIORITY",
            f"规则 {code} 的 priority 必须是整数",
            {"rule_code": code},
        )

    group = raw.get("conflict_group")
    if group is not None and not isinstance(group, str):
        raise RulesetCompileError(
            "INVALID_GROUP",
            f"规则 {code} 的 conflict_group 必须是字符串",
            {"rule_code": code},
        )

    return CompiledRule(
        code=code,
        name=str(raw.get("name") or code),
        description=str(raw.get("description") or ""),
        priority=priority,
        conflict_group=group,
        conflict_policy=policy,
        depends_on=tuple(dict.fromkeys(deps)),  # 去重保序
        condition=condition,
        action=action_expr,
        referenced_rules=referenced,
        order_index=index,
    )


def detect_cycle(
    by_code: dict[str, CompiledRule], start: str | None = None
) -> list[str] | None:
    """DFS 三色法检测环，返回形如 [a, b, a] 的环路径，无环返回 None。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {code: WHITE for code in by_code}
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for dep in by_code[node].depends_on:
            if dep not in by_code:
                continue
            if color[dep] == GRAY:
                idx = stack.index(dep)
                return stack[idx:] + [dep]
            if color[dep] == WHITE:
                found = dfs(dep)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    if start is not None:
        return dfs(start)
    for code in sorted(color):
        if color[code] == WHITE:
            found = dfs(code)
            if found:
                return found
    return None


def topo_order(compiled: CompiledRuleset) -> list[str]:
    """依赖优先的拓扑序；同层按提交顺序稳定排列。"""
    cycle = detect_cycle(compiled.by_code)
    if cycle:
        raise RulesetCompileError(
            "CYCLIC_DEPENDENCY",
            "规则依赖存在循环：" + " -> ".join(cycle),
            {"cycle": cycle},
        )

    result: list[str] = []
    visited: set[str] = set()

    def visit(code: str) -> None:
        if code in visited:
            return
        # 依赖按被依赖规则的提交顺序展开，保证输出确定性
        rule = compiled.by_code[code]
        ordered_deps = sorted(rule.depends_on, key=lambda c: compiled.by_code[c].order_index)
        for dep in ordered_deps:
            visit(dep)
        visited.add(code)
        result.append(code)

    for rule in sorted(compiled.rules, key=lambda r: r.order_index):
        visit(rule.code)
    return result
