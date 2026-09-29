"""基于 AST 白名单的安全表达式求值器。

条件/动作表达式被解析为 Python AST，但只允许一组明确白名单内的
节点、运算符、名字与可调用对象；不允许 import、属性穿透、lambda、
推导式、await 等任何可能逃逸沙箱的构造。

命名空间：
    doc                       —— 提交的文档（任意 JSON 值）
    <rule_code>.hit/.value   —— 其他规则的命中结果（由调用方注入）
"""

from __future__ import annotations

import ast
from typing import Any


class CompileError(ValueError):
    """表达式无法编译（语法错误或使用了白名单之外的构造）。"""


class EvaluationError(ValueError):
    """表达式在运行期求值失败（类型错误、非法调用等）。"""


# 允许的内置函数
_ALLOWED_FUNCS = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
}

# 额外的 DSL 函数名（在求值器中特化实现）
_DSL_FUNCS = {"lower", "upper", "contains"}

# 允许通过 .method 语法调用的字符串方法
_STR_METHODS = {"lower", "upper", "startswith", "endswith", "contains"}

# JSON 风格字面量：表达式里可直接写 true / false / null
_JSON_LITERALS = {"true": True, "false": False, "null": None}

# 白名单：表达式节点
_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Call,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Subscript,
    ast.IfExp,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.Starred,  # 仅在 Call 参数位置出现，语义由 Call 处理
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.USub,
    ast.UAdd,
    ast.Not,
    # 下标切片：x[1:3]
    ast.Slice,
)


def parse(expr: str) -> ast.Expression:
    """解析表达式并做静态白名单校验。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CompileError(f"表达式语法错误：{exc.msg}") from exc
    _validate_tree(tree)
    return tree


def referenced_names(expr: str) -> set[str]:
    """返回表达式中引用的顶层名字（排除内置/DSL 函数与 JSON 字面量）。"""
    tree = parse(expr)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if (
                node.id not in _ALLOWED_FUNCS
                and node.id not in _DSL_FUNCS
                and node.id not in _JSON_LITERALS
            ):
                names.add(node.id)
    return names


def _validate_tree(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise CompileError(
                f"表达式不允许使用该构造：{type(node).__name__}"
            )
        if isinstance(node, ast.Call):
            if node.keywords:
                raise CompileError("函数调用不支持关键字参数")
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            # attr 名字本身在求值期根据接收者类型决定是否允许，
            # 这里只做词法约束：禁止 dunder 穿透。
            if node.attr.startswith("_"):
                raise CompileError(f"禁止访问以下划线开头的属性：{node.attr}")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            # 禁止直接引用 dunder 名字（__import__、__builtins__ 等）
            if node.id.startswith("_"):
                raise CompileError(f"禁止访问以下划线开头的名字：{node.id}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # 只允许白名单内的字符串方法作为方法调用
            if node.func.attr not in _STR_METHODS:
                raise CompileError(f"不允许调用方法：{node.func.attr}")


def safe_eval(expr: str, namespaces: dict[str, Any] | None = None) -> Any:
    """求值已通过 parse 校验的表达式。

    namespaces 中包含 doc 以及各规则的 RuleRef。键缺失时读取得到 None
    （对应 JSON 中字段缺省），不抛 KeyError。
    """
    tree = parse(expr)
    env = dict(namespaces or {})

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)

        if isinstance(node, ast.Constant):
            return node.value

        if isinstance(node, ast.Name):
            if node.id in _ALLOWED_FUNCS or node.id in _DSL_FUNCS:
                return node.id  # 调用时按名字分派
            if node.id in _JSON_LITERALS:
                return _JSON_LITERALS[node.id]
            return env.get(node.id)

        if isinstance(node, ast.Attribute):
            return _attr(ev(node.value), node.attr)

        if isinstance(node, ast.Subscript):
            return _subscript(ev(node.value), ev(node.slice))

        if isinstance(node, ast.BinOp):
            return _binop(type(node.op), ev(node.left), ev(node.right))

        if isinstance(node, ast.UnaryOp):
            return _unaryop(type(node.op), ev(node.operand))

        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result: Any = True
                for value in node.values:
                    result = ev(value)
                    if not result:
                        return result
                return result
            # or
            result = False
            for value in node.values:
                result = ev(value)
                if result:
                    return result
            return result

        if isinstance(node, ast.Compare):
            left = ev(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = ev(comparator)
                if not _compare(type(op), left, right):
                    return False
                left = right
            return True

        if isinstance(node, ast.IfExp):
            return ev(node.body) if ev(node.test) else ev(node.orelse)

        if isinstance(node, ast.List):
            return [ev(e) for e in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(ev(e) for e in node.elts)
        if isinstance(node, ast.Set):
            return {ev(e) for e in node.elts}
        if isinstance(node, ast.Dict):
            return {
                ev(k) if k is not None else None: ev(v)
                for k, v in zip(node.keys, node.values)
            }

        if isinstance(node, ast.Call):
            return _call(ev(node.func), [ev(a) for a in node.args])

        if isinstance(node, ast.Slice):
            lower = ev(node.lower) if node.lower is not None else None
            upper = ev(node.upper) if node.upper is not None else None
            step = ev(node.step) if node.step is not None else None
            return slice(lower, upper, step)

        # Starred 只会出现在 Call 参数里，已在 _call 前被求值为普通值
        if isinstance(node, ast.Starred):
            return ev(node.value)

        raise EvaluationError(f"不支持的表达式节点：{type(node).__name__}")

    return ev(tree)


def _attr(value: Any, attr: str) -> Any:
    # 规则引用：is_vip.hit / is_vip.value
    if isinstance(value, RuleRef):
        if attr == "hit":
            return value.hit
        if attr == "value":
            return value.value
        raise EvaluationError(f"规则引用只支持 .hit / .value，收到：.{attr}")

    # 字典支持点访问：doc.foo 等同于 doc["foo"]
    if isinstance(value, dict):
        return value.get(attr)

    if isinstance(value, str):
        if attr in _STR_METHODS:
            return ("__str_method__", attr, value)
        raise EvaluationError(f"字符串不支持方法：{attr}")

    if value is None:
        return None

    raise EvaluationError(
        f"类型 {type(value).__name__} 不支持属性访问：.{attr}"
    )


def _subscript(value: Any, key: Any) -> Any:
    if value is None:
        return None
    if isinstance(key, slice):
        if isinstance(value, (str, list, tuple)):
            return value[key]
        raise EvaluationError("只有字符串/列表支持切片")
    if isinstance(value, dict):
        return value.get(key)
    if isinstance(value, (list, tuple, str)):
        try:
            return value[key]
        except (IndexError, TypeError):
            return None
    raise EvaluationError(f"类型 {type(value).__name__} 不支持下标访问")


def _compare(op: type, left: Any, right: Any) -> bool:
    try:
        if op is ast.Eq:
            return left == right
        if op is ast.NotEq:
            return left != right
        if op is ast.Is:
            return left is right
        if op is ast.IsNot:
            return left is not right
        if op is ast.In:
            return right is not None and left in right
        if op is ast.NotIn:
            return right is None or left not in right
        if left is None or right is None:
            # 数值比较中 None 视为缺失，不成立
            return False
        if op is ast.Lt:
            return left < right
        if op is ast.LtE:
            return left <= right
        if op is ast.Gt:
            return left > right
        if op is ast.GtE:
            return left >= right
    except TypeError as exc:
        raise EvaluationError(f"比较运算类型不匹配：{exc}") from exc
    raise EvaluationError(f"不支持的比较运算：{op.__name__}")


def _binop(op: type, left: Any, right: Any) -> Any:
    try:
        if op is ast.Add:
            return left + right
        if op is ast.Sub:
            return left - right
        if op is ast.Mult:
            return left * right
        if op is ast.Div:
            return left / right
        if op is ast.FloorDiv:
            return left // right
        if op is ast.Mod:
            return left % right
    except (TypeError, ZeroDivisionError) as exc:
        raise EvaluationError(f"算术运算错误：{exc}") from exc
    raise EvaluationError(f"不支持的算术运算：{op.__name__}")


def _unaryop(op: type, operand: Any) -> Any:
    if op is ast.Not:
        return not operand
    if op is ast.USub:
        return -operand
    if op is ast.UAdd:
        return +operand
    raise EvaluationError(f"不支持的一元运算：{op.__name__}")


def _call(func: Any, args: list[Any]) -> Any:
    # DSL 名字函数
    if isinstance(func, str):
        name = func
        if name in _ALLOWED_FUNCS:
            try:
                return _ALLOWED_FUNCS[name](*args)
            except (TypeError, ValueError) as exc:
                raise EvaluationError(f"{name}() 调用错误：{exc}") from exc
        if name == "lower" or name == "upper":
            if len(args) != 1:
                raise EvaluationError(f"{name}() 需要 1 个参数")
            if args[0] is None:
                return None
            if not isinstance(args[0], str):
                raise EvaluationError(f"{name}() 只接受字符串")
            return args[0].lower() if name == "lower" else args[0].upper()
        if name == "contains":
            if len(args) != 2:
                raise EvaluationError("contains() 需要 2 个参数")
            container, item = args
            return container is not None and item in container
        raise EvaluationError(f"未知函数：{name}")

    # 字符串方法：("__str_method__", name, receiver)
    if isinstance(func, tuple) and func and func[0] == "__str_method__":
        _, name, receiver = func
        if name == "contains":
            if len(args) != 1:
                raise EvaluationError("contains() 需要 1 个参数")
            return args[0] in receiver
        try:
            return getattr(receiver, name)(*args)
        except (TypeError, ValueError) as exc:
            raise EvaluationError(f"{name}() 调用错误：{exc}") from exc

    raise EvaluationError("只允许调用白名单函数")


class RuleRef:
    """注入到表达式命名空间的其他规则结果引用。"""

    __slots__ = ("hit", "value")

    def __init__(self, hit: bool, value: Any = None) -> None:
        self.hit = hit
        self.value = value
