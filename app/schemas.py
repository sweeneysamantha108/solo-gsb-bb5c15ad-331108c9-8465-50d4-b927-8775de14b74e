"""HTTP API 的 Pydantic 模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ConflictPolicy = Literal["collect_all", "priority", "first_hit"]


class RuleIn(BaseModel):
    code: str = Field(..., min_length=1, description="规则编码")
    name: str | None = None
    description: str | None = None
    priority: int = 0
    conflict_group: str | None = None
    conflict_policy: ConflictPolicy | None = None
    depends_on: list[str] = Field(default_factory=list)
    condition: str = Field("", description="条件表达式；空串视为恒真")
    action: str | None = Field(None, description="动作表达式，命中时求值")


class RulesetCreate(BaseModel):
    id: str | None = Field(None, description="可选自定义 ID")
    name: str = Field(..., min_length=1)
    description: str = ""
    default_conflict_policy: ConflictPolicy = "collect_all"
    rules: list[RuleIn] = Field(..., min_length=1)


class RulesetUpdate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    default_conflict_policy: ConflictPolicy = "collect_all"
    rules: list[RuleIn] = Field(..., min_length=1)


class RulesetMeta(BaseModel):
    id: str
    name: str
    description: str
    default_conflict_policy: ConflictPolicy
    current_version: int
    created_at: str
    updated_at: str


class RuleOut(BaseModel):
    code: str
    name: str
    description: str = ""
    priority: int = 0
    conflict_group: str | None = None
    conflict_policy: ConflictPolicy | None = None
    depends_on: list[str] = []
    condition: str = ""
    action: str | None = None
    referenced_rules: list[str] = []


class RulesetVersionOut(BaseModel):
    id: str
    ruleset_id: str
    version: int
    name: str
    description: str
    default_conflict_policy: ConflictPolicy
    rules: list[RuleOut]
    evaluation_order: list[str]
    created_at: str


# ---------- 规则集快照迁移（导出 / 导入） ----------


class SnapshotSource(BaseModel):
    ruleset_id: str = Field(..., description="导出时的源规则集 ID（仅作溯源，导入时不使用）")
    version: int = Field(
        ..., ge=1, description="导出时的源版本号（仅作溯源，导入时不沿用）"
    )


class SnapshotDigest(BaseModel):
    algorithm: Literal["sha256"] = Field(
        "sha256", description="内容摘要算法；当前仅支持 sha256"
    )
    value: str = Field(
        ..., min_length=1, description="规范化规则集内容的十六进制摘要"
    )


class SnapshotRulesetBody(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    default_conflict_policy: ConflictPolicy = "collect_all"
    rules: list[RuleIn] = Field(..., min_length=1)


class RulesetSnapshotPackage(BaseModel):
    format: Literal["ruleset-snapshot"] = Field(
        ..., description="固定标识 ruleset-snapshot"
    )
    format_version: Literal[1] = Field(1, description="快照包格式版本，当前为 1")
    source: SnapshotSource = Field(..., description="源规则集标识（溯源用）")
    exported_at: str | None = Field(
        None, description="导出时间（UTC ISO-8601）；不参与摘要，可为空"
    )
    digest: SnapshotDigest = Field(..., description="规范化内容摘要")
    ruleset: SnapshotRulesetBody = Field(
        ..., description="完整规则集定义（元数据 + 全部规则）"
    )


class SnapshotImportOut(BaseModel):
    action: Literal["created", "matched", "appended"] = Field(
        ...,
        description=(
            "created=目标不存在，已创建 v1；"
            "matched=目标已有内容一致的版本，直接返回该版本；"
            "appended=内容为新版本，已按目标规则集递增追加"
        ),
    )
    ruleset_id: str
    version: int = Field(..., description="目标规则集中实际对应的版本号")
    source: SnapshotSource = Field(..., description="回显包内源规则集标识")
    digest: SnapshotDigest = Field(..., description="回显核对通过的内容摘要")
    snapshot: RulesetVersionOut = Field(
        ..., description="目标版本的完整快照（结构与 GET 指定版本一致）"
    )


class EvaluateRequest(BaseModel):
    document: Any = Field(default_factory=dict)


class EvaluateBatchRequest(BaseModel):
    documents: list[Any] = Field(
        ...,
        max_length=100,
        description="待评估文档数组，单次最多 100 项",
    )
    version: int | None = Field(
        None, ge=1, description="整批固定使用的规则版本；缺省为最新版"
    )


class BatchEvaluationItem(BaseModel):
    index: int = Field(..., description="结果对应输入 documents 中的下标")
    status: Literal["success", "error"]
    result: EvaluationOut | None = Field(
        None, description="求值成功时的完整决策结果"
    )
    error: ErrorResponse | None = Field(
        None, description="该项求值失败的错误信息，不影响其他项"
    )


class BatchEvaluationOut(BaseModel):
    ruleset_id: str
    version: int = Field(..., description="整批实际使用的规则版本")
    total: int
    success_count: int
    error_count: int
    results: list[BatchEvaluationItem]


class ConflictExplanation(BaseModel):
    group: str | None = None
    policy: str
    winner: str | None = None
    suppressed: list[dict[str, Any]] = []
    field: str | None = None
    previous_winner: str | None = None
    reason: str | None = None


class RuleTrace(BaseModel):
    rule_code: str
    hit: bool
    value: Any = None
    error: str | None = None


class EvaluationOut(BaseModel):
    id: str
    ruleset_id: str
    version: int
    document: Any
    fired_rule_codes: list[str]
    hit_rule_codes: list[str]
    evaluation_order: list[str]
    iterations: int
    converged: bool
    decision: dict[str, Any]
    conflicts: list[dict[str, Any]]
    traces: list[dict[str, Any]]
    created_at: str


class CoverageRequest(BaseModel):
    version: int = Field(
        ..., ge=1, description="分析所基于的规则版本（必填，不可变快照）"
    )
    documents: list[Any] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="待评估文档数组，1～100 项",
    )


class CoverageUnitOut(BaseModel):
    kind: Literal["rule", "group", "field"] = Field(
        ..., description="rule=规则命中；group=冲突组裁决；field=同名字段覆盖"
    )
    rule_code: str | None = Field(None, description="kind=rule 时的规则编码")
    group: str | None = Field(None, description="kind=group 时的冲突组名")
    field: str | None = Field(None, description="kind=field 时被覆盖的决策字段名")
    winner: str | None = Field(None, description="group 胜出方 / field 的最终写入方")
    previous_winner: str | None = Field(
        None, description="kind=field 时覆盖前的写入规则；其余为 null"
    )
    suppressed_rule_codes: list[str] = Field(
        default_factory=list,
        description="被压制规则编码（按编码排序；field 单元恒为空）",
    )


class ConflictScenarioOut(BaseModel):
    kind: Literal["group", "field"]
    group: str | None = Field(None, description="kind=group 时的冲突组名")
    field: str | None = Field(None, description="kind=field 时被覆盖的字段名")
    policy: str | None = Field(None, description="观察到该场景时的冲突策略")
    winner: str | None = Field(None, description="group 胜出方 / field 最终写入方")
    suppressed_rule_codes: list[str] = Field(
        default_factory=list, description="被压制规则编码（按编码排序）"
    )
    previous_winner: str | None = Field(
        None, description="field 覆盖前的写入规则；group 场景为 null"
    )
    reason: str | None = Field(
        None, description="field 覆盖原因（如 field_overwritten）；group 场景为 null"
    )
    observed_count: int = Field(..., description="该场景在多少份成功文档中出现")
    observed_at_indexes: list[int] = Field(
        ..., description="出现该场景的输入下标（升序）"
    )
    coverage_units: list[CoverageUnitOut] = Field(
        ..., description="该场景贡献的覆盖单元（含涉及规则的 rule 单元）"
    )


class RuleCoverageOut(BaseModel):
    code: str = Field(..., description="规则编码（按 evaluation_order 排列）")
    hit_count: int = Field(..., description="该规则在成功文档中的命中次数")
    covered_by_indexes: list[int] = Field(
        ..., description="命中该规则的成功项输入下标（升序）"
    )


class CoverageItemOut(BaseModel):
    index: int = Field(..., description="对应输入 documents 中的下标")
    status: Literal["success", "error"]
    fired_rule_codes: list[str] = Field(
        default_factory=list, description="成功项的命中规则（含被压制者）；失败项为空"
    )
    conflicts: list[dict[str, Any]] = Field(
        default_factory=list, description="成功项观察到的原始冲突解释；失败项为空"
    )
    coverage_units: list[CoverageUnitOut] = Field(
        default_factory=list, description="该项贡献的覆盖单元；失败项为空"
    )
    error: ErrorResponse | None = Field(
        None, description="该项求值失败时的错误信息；不影响其他项与覆盖汇总"
    )


class CoverageOut(BaseModel):
    ruleset_id: str
    version: int = Field(..., description="整批实际使用的规则版本（不可变快照）")
    total: int = Field(..., description="输入文档总数（1～100）")
    success_count: int
    error_count: int
    rule_coverage: list[RuleCoverageOut] = Field(
        ..., description="该版本全部规则的命中统计，按 evaluation_order 排列"
    )
    uncovered_rule_codes: list[str] = Field(
        ..., description="所有成功文档均未命中的规则编码，按 evaluation_order 排列"
    )
    conflict_scenarios: list[ConflictScenarioOut] = Field(
        ..., description="去重后的已观察冲突场景，按首次观察顺序排列"
    )
    representative_indexes: list[int] = Field(
        ...,
        description=(
            "确定性贪心选出的代表输入下标：每轮新增覆盖单元最多、并列取最小下标，"
            "直至无新增；失败项不参与选择"
        ),
    )
    items: list[CoverageItemOut] = Field(
        ..., description="逐项结果（含失败项错误），按原输入下标返回"
    )


class RecomputeRequest(BaseModel):
    document: Any | None = Field(
        None, description="为空则使用该规则集上一次决策的文档"
    )


class CompareRequest(BaseModel):
    base_version: int = Field(
        ..., ge=1, description="作为比较基准的规则版本（必填，不可变快照）"
    )
    target_version: int = Field(
        ..., ge=1, description="待发布预演的目标规则版本（必填，不可变快照）"
    )
    document: Any = Field(default_factory=dict)


class DiffEntryOut(BaseModel):
    path: str = Field(..., description="RFC 6901 JSON Pointer；根差异为 \"\"")
    old: Any | None = Field(None, description="基准版侧的值；added 时为 null")
    new: Any | None = Field(None, description="目标版侧的值；removed 时为 null")


class DecisionDiffOut(BaseModel):
    added: list[DiffEntryOut] = Field(..., description="仅目标版存在的叶子，按路径排序")
    removed: list[DiffEntryOut] = Field(..., description="仅基准版存在的叶子，按路径排序")
    changed: list[DiffEntryOut] = Field(..., description="同路径值/类型变化，按路径排序")


class RuleListDiffOut(BaseModel):
    base: list[str] = Field(..., description="基准版的编码列表（保序回显）")
    target: list[str] = Field(..., description="目标版的编码列表（保序回显）")
    added: list[str] = Field(..., description="相对基准版新增的编码")
    removed: list[str] = Field(..., description="相对基准版消失的编码")


class ConflictDiffOut(BaseModel):
    base: list[dict[str, Any]] = Field(..., description="基准版全部冲突解释")
    target: list[dict[str, Any]] = Field(..., description="目标版全部冲突解释")
    added: list[dict[str, Any]] = Field(
        ..., description="目标版新增的冲突组/字段覆盖，按组名/字段名排序"
    )
    removed: list[dict[str, Any]] = Field(
        ..., description="目标版消失的冲突组/字段覆盖，按组名/字段名排序"
    )
    changed: list[dict[str, Any]] = Field(
        ..., description="同组/同字段但胜出方或被压制方变化的冲突"
    )


class CompareSummaryOut(BaseModel):
    fired_rules: RuleListDiffOut = Field(..., description="命中评估为真的规则（含被压制者）")
    hit_rules: RuleListDiffOut = Field(..., description="冲突处理后最终生效的规则")
    conflicts: ConflictDiffOut = Field(..., description="冲突组与字段覆盖的跨版本摘要")


class CompareSide(BaseModel):
    version: int = Field(..., description="该侧实际求值使用的规则版本")
    status: Literal["success", "error"]
    result: EvaluationOut | None = Field(
        None, description="求值成功时的完整决策结果（不落库，id 为空）"
    )
    error: ErrorResponse | None = Field(
        None, description="该侧求值失败时的错误信息；不影响另一侧"
    )


class CompareOut(BaseModel):
    ruleset_id: str
    base_version: int
    target_version: int
    base: CompareSide = Field(..., description="基准版一侧的求值结果")
    target: CompareSide = Field(..., description="目标版一侧的求值结果")
    summary: CompareSummaryOut | None = Field(
        None, description="命中规则/冲突摘要；任一侧求值失败时为 null"
    )
    decision_diff: DecisionDiffOut | None = Field(
        None, description="按 JSON 路径归并的决策差异；任一侧求值失败时为 null"
    )


class ImpactRequest(BaseModel):
    version: int = Field(
        ..., ge=1, description="分析所基于的规则版本（必填，不可变快照）"
    )
    rule_codes: list[str] = Field(
        ...,
        min_length=1,
        description="一个或多个待分析的规则编码",
    )


class ImpactedRuleOut(BaseModel):
    rule_code: str = Field(..., description="规则编码")
    level: int = Field(
        ...,
        description="受影响层级：0=输入规则，1=直接依赖方，>=2=传递依赖方",
    )
    relation: Literal["input", "direct_dependent", "transitive_dependent"]


class CoupledRuleOut(BaseModel):
    rule_code: str = Field(..., description="联动规则编码")
    conflict_group: str = Field(..., description="所属冲突组")
    coupled_with: str = Field(
        ..., description="evaluation_order 上第一个触发联动的受影响规则"
    )
    level: None = Field(
        None, description="联动关系不经依赖边传播，故无层级，恒为 null"
    )


class ImpactOut(BaseModel):
    ruleset_id: str
    version: int = Field(..., description="实际分析的规则版本")
    rule_codes: list[str] = Field(
        ..., description="去重保序后的输入规则编码回显"
    )
    affected: list[ImpactedRuleOut] = Field(
        ...,
        description=(
            "输入规则（level=0）与全部受影响规则，按 evaluation_order 排列；"
            "包含输入、direct_dependents、transitive_dependents 三类"
        ),
    )
    direct_dependents: list[str] = Field(
        ..., description="直接依赖任一输入规则的规则，按 evaluation_order 排列"
    )
    transitive_dependents: list[str] = Field(
        ...,
        description="传递依赖（层级 >=2）的规则，按 evaluation_order 排列",
    )
    coupled_rules: list[CoupledRuleOut] = Field(
        ...,
        description=(
            "与受影响规则处于同一 conflict_group 的其余规则；不经依赖边传播，"
            "按 evaluation_order 排列"
        ),
    )


class FootprintRequest(BaseModel):
    version: int = Field(
        ..., ge=1, description="分析所基于的规则版本（必填，不可变快照）"
    )
    rule_codes: list[str] = Field(
        ...,
        min_length=1,
        description="一个或多个待分析的目标规则编码（非空，重复编码去重保序）",
    )


class DocPathOut(BaseModel):
    path: str = Field(
        ...,
        description="doc 为根的访问链转义出的 RFC 6901 JSON Pointer；空串表示整个文档",
    )
    used_by: list[str] = Field(
        ...,
        description=(
            "目标规则及其上游闭包中实际读取该路径的规则编码，"
            "按 evaluation_order 排列并去重"
        ),
    )


class RuleFootprintOut(BaseModel):
    rule_code: str = Field(..., description="目标规则编码")
    paths: list[DocPathOut] = Field(
        ...,
        description=(
            "该目标规则自身及全部上游依赖读取的文档路径，去重后按 JSON Pointer 字典序排列"
        ),
    )


class FootprintOut(BaseModel):
    ruleset_id: str
    version: int = Field(..., description="实际分析的规则版本")
    rule_codes: list[str] = Field(
        ..., description="去重保序后的输入规则编码回显"
    )
    footprints: list[RuleFootprintOut] = Field(
        ..., description="每个目标规则的输入足迹，与 rule_codes 同序"
    )


class RuleStatOut(BaseModel):
    code: str = Field(..., description="规则编码")
    hit_count: int = Field(..., description="traces 中 hit=true 的决策条数")
    hit_rate: float = Field(
        ..., description="hit_count / total_evaluations，保留 4 位小数"
    )


class DecisionFieldCountOut(BaseModel):
    field: str = Field(..., description="决策结果的顶层字段名")
    count: int = Field(..., description="该字段在多少条决策结果中出现")


class EvaluationStatsOut(BaseModel):
    ruleset_id: str
    version: int | None = Field(None, description="回显 version 筛选条件")
    since: str | None = Field(
        None, description="回显归一化（UTC 秒级）后的 since 筛选条件"
    )
    until: str | None = Field(
        None, description="回显归一化（UTC 秒级）后的 until 筛选条件"
    )
    total_evaluations: int = Field(..., description="筛选后命中的持久化决策条数")
    rule_stats: list[RuleStatOut] = Field(
        ..., description="按规则编码排序；无匹配记录时为空数组"
    )
    decision_field_counts: list[DecisionFieldCountOut] = Field(
        ..., description="按字段名排序；无匹配记录时为空数组"
    )


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}
