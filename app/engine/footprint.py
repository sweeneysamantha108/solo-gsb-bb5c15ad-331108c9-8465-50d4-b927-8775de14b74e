"""规则输入足迹（input footprint）静态分析。

在**不执行任何文档求值、不写库**的前提下，汇总目标规则自身及其全部
上游依赖规则在 ``condition`` / ``action`` 表达式中**读取的文档路径**：

1. 解析表达式 AST，只识别以 ``doc`` 为根的访问链（下标与字典点访问，
   后者在求值期与下标等价，见 ``ast_eval._attr``）；
2. 访问链的每一级下标必须是**字符串或非负整数常量**，据此按
   RFC 6901 转义为 JSON Pointer（``~`` -> ``~0``、``/`` -> ``~1``）；
3. 任何一级下标是名字、运算、调用、切片、负数等动态/不支持形式时，
   无法静态确定读取路径，直接抛 ``UNSUPPORTED_DOCUMENT_ACCESS``，
   不产生任何部分结果；
4. 上游闭包沿 ``depends_on``（并防御性并入条件/动作中的规则引用）展开；
5. 每条路径给出 ``used_by``：闭包内实际读取该路径的规则编码，
   按该版本 ``evaluation_order`` 排列；路径按 JSON Pointer 字典序排列。

注意：静态分析是 over-approximation——三元表达式的两个分支、
``and/or`` 的短路侧等实际未必执行的读取都会被计入，这正是“足迹”
所需的保守语义。
"""

from __future__ import annotations

import ast
from typing import Optional, Union

from .ast_eval import parse, referenced_names
from .compiler import CompiledRuleset

# _doc_chain 的哨兵返回值：访问链以 doc 为根，但含有无法静态确定的下标
_DYNAMIC = object()

# 访问链分析结果：常量下标元组（含空元组=doc 根）、None（非 doc 根）
# 或 _DYNAMIC（doc 根但含动态下标）
Chain = Optional[tuple[Union[str, int], ...]]


class FootprintAnalysisError(ValueError):
    """输入足迹分析无法完成（未知规则 / 不支持的动态文档访问）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def escape_token(token: str) -> str:
    """按 RFC 6901 转义单个引用词：``~`` -> ``~0``，``/`` -> ``~1``。"""
    return token.replace("~", "~0").replace("/", "~1")


def json_pointer(
    segments: tuple[Union[str, int], ...],
) -> str:
    """把常量下标序列拼成 RFC 6901 JSON Pointer；空序列为整个文档。"""
    return "".join("/" + escape_token(str(seg)) for seg in segments)


def _constant_index(node: ast.AST) -> Union[str, int, None]:
    """静态下标：字符串或非负整数常量；其他形式一律返回 None。

    bool/float/负数/切片等都不是可静态确定的文档下标。
    """
    # Python < 3.9 的 Subscript.slice 包一层 ast.Index
    if hasattr(ast, "Index") and isinstance(node, ast.Index):  # pragma: no cover
        node = node.value  # type: ignore[attr-defined]
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):  # True/False 不是合法文档下标
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _doc_chain(node: ast.AST) -> Chain:
    """若 node 是一条以 ``doc`` 为根的访问链，返回各级常量下标元组。

    - 以 ``doc`` 为根但某级下标无法静态确定 -> ``_DYNAMIC``；
    - 不是 doc 根（规则引用、普通名字、字面量等）-> ``None``。

    字典点访问 ``doc.foo`` 在求值期等价于 ``doc["foo"]``，按字符串下标处理。
    """
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        return () if node.id == "doc" else None
    if isinstance(node, ast.Subscript):
        base = _doc_chain(node.value)
        if base is None:
            return None
        if base is _DYNAMIC:
            return _DYNAMIC
        key = _constant_index(node.slice)
        if key is None:
            return _DYNAMIC
        return (*base, key)  # type: ignore[arg-type]
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        base = _doc_chain(node.value)
        if base is None:
            return None
        if base is _DYNAMIC:
            return _DYNAMIC
        return (*base, node.attr)  # type: ignore[arg-type]
    return None


def extract_doc_paths(
    expr: str, rule_code: str = "<expression>", part: str = "expression"
) -> set[str]:
    """提取单个表达式读取的全部 doc 路径（JSON Pointer 集合）。

    只收集**完整访问链**（叶子）：``doc["a"]["b"]`` 读取的文档路径是
    ``/a/b``——中间的 ``doc["a"]`` 只是沿结构导航，不是独立输入字段；
    方法调用接收者（``doc["s"].upper()``）在方法名之前终止，路径为
    ``/s``；单独出现的裸 ``doc``（如 ``len(doc)``）记为 ``""``（整个文档）。

    访问链上出现任何动态/不支持下标时抛 ``UNSUPPORTED_DOCUMENT_ACCESS``。
    """
    tree = parse(expr)

    # 记录父节点：完整链末端是“不被另一条 doc 访问继续向下延伸”的节点
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            base = _doc_chain(node.value)
            if (
                base is not None
                and base is not _DYNAMIC
                and _constant_index(node.slice) is None
            ):
                # 恰在该节点出现无法静态确定的下标——精确定位动态来源
                index_src = ast.get_source_segment(expr, node.slice) or ""
                raise FootprintAnalysisError(
                    "UNSUPPORTED_DOCUMENT_ACCESS",
                    (
                        f"规则 {rule_code} 的 {part} 表达式包含无法静态确定的"
                        f"文档下标：doc[...{index_src}...]"
                    ),
                    {
                        "rule_code": rule_code,
                        "part": part,
                        "index": index_src,
                        "expression": expr,
                    },
                )

        chain = _doc_chain(node)
        if chain is None or chain is _DYNAMIC:
            continue

        # 完整链末端判定：父节点若继续沿 doc 链向下访问，则当前节点只是
        # 中间导航节点（doc["a"] in doc["a"]["b"]）。doc["s"].upper() 的
        # .upper 是字符串方法调用而非文档字段，该 Attribute 本身跳过，
        # 链在其接收者处终止。
        parent = parents.get(node)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(parent, ast.Call)
            and parent.func is node
        ):
            continue
        if (
            parent is not None
            and isinstance(parent, (ast.Subscript, ast.Attribute))
            and parent.value is node
        ):
            grandparent = parents.get(parent)
            method_call = (
                isinstance(parent, ast.Attribute)
                and isinstance(grandparent, ast.Call)
                and grandparent.func is parent
            )
            if not method_call:
                continue

        paths.add(json_pointer(chain))  # type: ignore[arg-type]

    return paths


def _upstream_closure(compiled: CompiledRuleset, code: str) -> set[str]:
    """目标规则自身 + 沿依赖边展开的全部上游规则。

    依赖边取 depends_on 与条件引用，并防御性并入动作表达式中的规则引用。
    """
    seen = {code}
    stack = [code]
    while stack:
        current = stack.pop()
        rule = compiled.by_code[current]
        refs = set(rule.depends_on) | set(rule.referenced_rules)
        # 动作中的规则引用同样构成上游（求值期命名空间对所有规则可见）
        if rule.action:
            refs |= {
                name
                for name in referenced_names(rule.action)
                if name in compiled.by_code
            }
        for ref in refs:
            if ref not in seen:
                seen.add(ref)
                stack.append(ref)
    return seen


def input_footprint(
    compiled: CompiledRuleset, rule_codes: list[str]
) -> dict[str, object]:
    """计算每个目标规则及其上游闭包读取的文档路径。

    返回 ``rule_codes``（去重保序回显）与 ``footprints``（与输入同序）：
    每项含去重后的 ``paths``（JSON Pointer 字典序）及每条路径的
    ``used_by`` 规则编码（按 evaluation_order 排列）。
    """
    inputs = list(dict.fromkeys(rule_codes))
    missing = [code for code in inputs if code not in compiled.by_code]
    if missing:
        raise FootprintAnalysisError(
            "RULE_NOT_FOUND",
            f"规则编码在该版本中不存在：{missing}",
            {"rule_codes": missing},
        )

    order = compiled.evaluation_order

    # 各目标的上游闭包；并集决定本次需要扫描的规则范围
    closures: dict[str, set[str]] = {}
    scanned: set[str] = set()
    for target in inputs:
        closure = _upstream_closure(compiled, target)
        closures[target] = closure
        scanned |= closure

    # 统一按 evaluation_order 扫描：报错规则确定，且每个规则只解析一次
    rule_paths: dict[str, set[str]] = {}
    for code in order:
        if code not in scanned:
            continue
        rule = compiled.by_code[code]
        paths: set[str] = set()
        if rule.condition:
            paths |= extract_doc_paths(
                rule.condition, rule_code=code, part="condition"
            )
        if rule.action:
            paths |= extract_doc_paths(
                rule.action, rule_code=code, part="action"
            )
        rule_paths[code] = paths

    footprints: list[dict[str, object]] = []
    for target in inputs:
        closure_order = [code for code in order if code in closures[target]]
        path_users: dict[str, set[str]] = {}
        for code in closure_order:
            for path in rule_paths[code]:
                path_users.setdefault(path, set()).add(code)
        footprints.append(
            {
                "rule_code": target,
                "paths": [
                    {
                        "path": path,
                        "used_by": [
                            code for code in closure_order if code in path_users[path]
                        ],
                    }
                    for path in sorted(path_users)
                ],
            }
        )

    return {"rule_codes": inputs, "footprints": footprints}
