"""规则引擎：安全表达式、编译校验、定点求值与冲突处理。"""

from .ast_eval import (
    CompileError,
    EvaluationError,
    RuleRef,
    safe_eval,
)
from .compiler import (
    CompiledRule,
    CompiledRuleset,
    RulesetCompileError,
    compile_ruleset,
    detect_cycle,
    topo_order,
)
from .coverage import build_coverage
from .diff import compare_results, conflict_diff, decision_diff, list_diff
from .evaluator import EvaluationFailed, evaluate
from .footprint import FootprintAnalysisError, input_footprint
from .impact import ImpactAnalysisError, impact_analysis, reverse_dependencies

__all__ = [
    "CompileError",
    "EvaluationError",
    "RuleRef",
    "safe_eval",
    "CompiledRule",
    "CompiledRuleset",
    "RulesetCompileError",
    "compile_ruleset",
    "detect_cycle",
    "topo_order",
    "build_coverage",
    "EvaluationFailed",
    "evaluate",
    "compare_results",
    "conflict_diff",
    "decision_diff",
    "list_diff",
    "FootprintAnalysisError",
    "input_footprint",
    "ImpactAnalysisError",
    "impact_analysis",
    "reverse_dependencies",
]
