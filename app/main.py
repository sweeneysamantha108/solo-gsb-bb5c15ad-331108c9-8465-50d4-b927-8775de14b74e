"""FastAPI 应用：规则集版本化管理与规则决策计算。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from . import crud
from .database import connect, init_db
from .engine import (
    CompiledRuleset,
    EvaluationFailed,
    FootprintAnalysisError,
    ImpactAnalysisError,
    RulesetCompileError,
    build_coverage,
    compare_results,
    compile_ruleset,
    decision_diff,
    evaluate,
    impact_analysis,
    input_footprint,
)
from .schemas import (
    BatchEvaluationOut,
    CompareOut,
    CompareRequest,
    CoverageOut,
    CoverageRequest,
    ErrorResponse,
    EvaluateBatchRequest,
    EvaluateRequest,
    EvaluationOut,
    EvaluationStatsOut,
    FootprintOut,
    FootprintRequest,
    ImpactOut,
    ImpactRequest,
    RecomputeRequest,
    RulesetCreate,
    RulesetMeta,
    RulesetSnapshotPackage,
    RulesetUpdate,
    RulesetVersionOut,
    SnapshotImportOut,
)
from .seed import seed_demo


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = connect()
    try:
        init_db(conn)
        if os.environ.get("SEED_DEMO", "").strip() in ("1", "true", "TRUE", "yes"):
            seed_demo(conn)
            conn.commit()
    finally:
        conn.close()
    yield


app = FastAPI(
    title="规则决策服务",
    description=(
        "创建带条件依赖与冲突策略的规则集，提交文档按依赖图求稳定结果，"
        "返回命中顺序与冲突解释；支持循环依赖检测与规则集版本化。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------- 依赖 ----------


def get_db() -> sqlite3.Connection:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 异常映射 ----------


@app.exception_handler(RulesetCompileError)
async def handle_compile_error(_: Request, exc: RulesetCompileError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(EvaluationFailed)
async def handle_evaluation_failed(_: Request, exc: EvaluationFailed) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


def _error(code: str, message: str, status: int, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _iso_utc_bound(value: datetime | None) -> str | None:
    """把查询时间归一到 UTC 秒级字符串（与 created_at 存储格式一致，字典序可比较）。

    无时区的输入按 UTC 处理。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------- 工具 ----------


def _ruleset_meta(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "default_conflict_policy": row["default_conflict_policy"],
        "current_version": row["current_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _version_out(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": snapshot["id"],
        "ruleset_id": snapshot["ruleset_id"],
        "version": snapshot["version"],
        "name": snapshot["name"],
        "description": snapshot["description"],
        "default_conflict_policy": snapshot["default_conflict_policy"],
        "evaluation_order": snapshot["evaluation_order"],
        "created_at": snapshot["created_at"],
        "rules": [
            {
                "code": r["code"],
                "name": r["name"],
                "description": r.get("description", ""),
                "priority": r.get("priority", 0),
                "conflict_group": r.get("conflict_group"),
                "conflict_policy": r.get("conflict_policy"),
                "depends_on": r.get("depends_on", []),
                "condition": r.get("condition", ""),
                "action": r.get("action"),
                "referenced_rules": r.get("referenced_rules", []),
            }
            for r in snapshot["rules"]
        ],
    }


def _load_compiled(
    conn: sqlite3.Connection, ruleset_id: str, version: int
) -> tuple[dict[str, Any], CompiledRuleset]:
    """读取指定版本快照并编译为可求值对象。"""
    snapshot = crud.get_version(conn, ruleset_id, version)
    if snapshot is None:
        raise LookupError(f"规则集 {ruleset_id} 的版本 {version} 不存在")
    compiled = compile_ruleset(
        snapshot["name"],
        snapshot["description"],
        snapshot["default_conflict_policy"],
        snapshot["rules"],
    )
    return snapshot, compiled


# ---------- 健康检查 ----------


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- 规则集 ----------


@app.post(
    "/api/rulesets",
    response_model=RulesetVersionOut,
    status_code=201,
    tags=["rulesets"],
    responses={400: {"model": ErrorResponse}},
)
def create_ruleset(
    payload: RulesetCreate, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    data = payload.model_dump()
    try:
        ruleset_id, version, _snapshot = crud.create_ruleset(conn, data)
    except KeyError as exc:
        return _error("RULESET_ID_EXISTS", str(exc.args[0]), 409)
    except crud.InvalidCustomId as exc:
        return _error("INVALID_ID", str(exc), 400)
    snapshot = crud.get_version(conn, ruleset_id, version)
    return _version_out(snapshot)


@app.get("/api/rulesets", response_model=list[RulesetMeta], tags=["rulesets"])
def list_rulesets(conn: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    return [_ruleset_meta(r) for r in crud.list_rulesets(conn)]


@app.get(
    "/api/rulesets/{ruleset_id}",
    response_model=RulesetMeta,
    tags=["rulesets"],
    responses={404: {"model": ErrorResponse}},
)
def get_ruleset(
    ruleset_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> Any:
    row = crud.get_ruleset(conn, ruleset_id)
    if row is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    return _ruleset_meta(row)


@app.put(
    "/api/rulesets/{ruleset_id}",
    response_model=RulesetVersionOut,
    tags=["rulesets"],
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def update_ruleset(
    ruleset_id: str,
    payload: RulesetUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    if crud.get_ruleset(conn, ruleset_id) is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    new_version = crud.update_ruleset(conn, ruleset_id, payload.model_dump())
    snapshot = crud.get_version(conn, ruleset_id, new_version)
    return _version_out(snapshot)


@app.get(
    "/api/rulesets/{ruleset_id}/versions",
    tags=["rulesets"],
    responses={404: {"model": ErrorResponse}},
)
def list_versions(
    ruleset_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> Any:
    if crud.get_ruleset(conn, ruleset_id) is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    rows = crud.list_versions(conn, ruleset_id)
    return [
        {
            "id": r["id"],
            "ruleset_id": r["ruleset_id"],
            "version": r["version"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get(
    "/api/rulesets/{ruleset_id}/versions/{version}",
    response_model=RulesetVersionOut,
    tags=["rulesets"],
    responses={404: {"model": ErrorResponse}},
)
def get_version(
    ruleset_id: str,
    version: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    snapshot = crud.get_version(conn, ruleset_id, version)
    if snapshot is None:
        if crud.get_ruleset(conn, ruleset_id) is None:
            return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
        return _error("VERSION_NOT_FOUND", f"版本 {version} 不存在", 404)
    return _version_out(snapshot)


# ---------- 规则集快照迁移（导出 / 导入） ----------


@app.get(
    "/api/rulesets/{ruleset_id}/versions/{version}/snapshot",
    response_model=RulesetSnapshotPackage,
    tags=["ruleset-migration"],
    responses={404: {"model": ErrorResponse}},
)
def export_snapshot(
    ruleset_id: str,
    version: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """导出指定版本为快照迁移包：源规则集标识 + 完整规则定义 + 规范化内容摘要。

    包为自描述 JSON：``format`` / ``format_version`` 标识包格式，
    ``source`` 记录导出时的规则集 ID 与版本（仅溯源），``digest`` 为对
    ruleset 规范化内容的 sha256 摘要，``exported_at`` 不参与摘要。
    规则集或版本不存在返回 404。
    """
    snapshot = crud.get_version(conn, ruleset_id, version)
    if snapshot is None:
        if crud.get_ruleset(conn, ruleset_id) is None:
            return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
        return _error("VERSION_NOT_FOUND", f"版本 {version} 不存在", 404)
    return crud.build_snapshot_package(snapshot, ruleset_id)


@app.post(
    "/api/rulesets/{ruleset_id}/snapshot-import",
    response_model=SnapshotImportOut,
    tags=["ruleset-migration"],
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def import_snapshot(
    ruleset_id: str,
    package: RulesetSnapshotPackage,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """导入快照迁移包：核对摘要、重新编译，幂等落地到目标规则集。

    流程（任何一步失败均不写库）：

    1. 校验包格式与摘要算法，重算规范化内容的 sha256，与包内摘要不符
       返回 400 ``DIGEST_MISMATCH``；
    2. 按包内完整规则定义**重新编译**（重复编码、未知依赖、循环依赖、
       冲突策略冲突等静态校验），失败返回 400 对应编译错误码；
    3. 目标 ID 不存在 → 创建 v1（``action=created``，HTTP 201）；
       目标已存在且内容与**任一版本**一致 → 幂等返回该版本
       （``action=matched``，HTTP 200，不产生新版本）；
       否则按**目标规则集** current_version+1 追加新版本
       （``action=appended``，HTTP 200）——版本号始终按目标规则集
       递增，不采用包内 ``source.version``。

    写入在单连接事务内完成（提交/回滚由 get_db 统一处理），原子化。
    目标 ID 非法返回 400 ``INVALID_ID``。
    """
    if not crud.valid_custom_id(ruleset_id):
        return _error(
            "INVALID_ID", "目标规则集 id 只能包含字母、数字、-、_", 400
        )
    try:
        action, version, snapshot = crud.import_snapshot(
            conn, ruleset_id, package.model_dump()
        )
    except crud.SnapshotPackageError as exc:
        return _error(exc.code, exc.message, 400, details=exc.details)

    if action == "created":
        response.status_code = 201
    return {
        "action": action,
        "ruleset_id": ruleset_id,
        "version": version,
        "source": package.source.model_dump(),
        "digest": package.digest.model_dump(),
        "snapshot": _version_out(snapshot),
    }


# ---------- 静态影响分析 ----------


@app.post(
    "/api/rulesets/{ruleset_id}/impact",
    response_model=ImpactOut,
    tags=["rulesets"],
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def ruleset_impact(
    ruleset_id: str,
    req: ImpactRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """静态依赖影响分析：不执行文档求值、不写入 evaluations。

    基于指定版本快照中已编译的 depends_on 与条件表达式引用，计算输入规则的
    反向传递依赖闭包（direct/transitive dependents），并标记同 conflict_group
    的联动规则。规则集或版本不存在返回 404；规则编码不存在返回 422。
    """
    if crud.get_ruleset(conn, ruleset_id) is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    try:
        _snapshot, compiled = _load_compiled(conn, ruleset_id, req.version)
    except LookupError:
        return _error("VERSION_NOT_FOUND", f"版本 {req.version} 不存在", 404)

    try:
        result = impact_analysis(compiled, req.rule_codes)
    except ImpactAnalysisError as exc:
        return _error(exc.code, exc.message, 422, details=exc.details)

    return {"ruleset_id": ruleset_id, "version": req.version, **result}


# ---------- 静态输入足迹分析 ----------


@app.post(
    "/api/rulesets/{ruleset_id}/input-footprint",
    response_model=FootprintOut,
    tags=["rulesets"],
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def ruleset_input_footprint(
    ruleset_id: str,
    req: FootprintRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """静态汇总目标规则及其上游依赖读取的文档路径：不求值、不写库。

    仅接受以 doc 为根、每级下标为字符串/非负整数常量的访问链，并按
    RFC 6901 转义为 JSON Pointer；发现动态下标返回
    422 UNSUPPORTED_DOCUMENT_ACCESS（不给部分结果）。未知规则沿用
    impact 的 422 RULE_NOT_FOUND。
    """
    if crud.get_ruleset(conn, ruleset_id) is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    try:
        _snapshot, compiled = _load_compiled(conn, ruleset_id, req.version)
    except LookupError:
        return _error("VERSION_NOT_FOUND", f"版本 {req.version} 不存在", 404)

    try:
        result = input_footprint(compiled, req.rule_codes)
    except FootprintAnalysisError as exc:
        return _error(exc.code, exc.message, 422, details=exc.details)

    return {"ruleset_id": ruleset_id, "version": req.version, **result}


# ---------- 决策计算 ----------


def _persist_evaluation(
    conn: sqlite3.Connection,
    ruleset_id: str,
    target_version: int,
    document: Any,
    result: dict[str, Any],
    persist: bool,
) -> dict[str, Any]:
    """按 persist 决定是否落库；不落库时返回与落库同构的记录（id 为空）。"""
    if persist:
        return crud.save_evaluation(conn, ruleset_id, target_version, document, result)
    return {
        "id": "",
        "ruleset_id": ruleset_id,
        "version": target_version,
        "document": document,
        "created_at": crud.now(),
        **result,
    }


@app.post(
    "/api/rulesets/{ruleset_id}/evaluate",
    response_model=EvaluationOut,
    tags=["evaluation"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
def evaluate_document(
    ruleset_id: str,
    req: EvaluateRequest,
    version: int | None = Query(None, ge=1, description="指定版本；默认最新版"),
    persist: bool = Query(True, description="是否持久化本次结果"),
    idempotency_key: str | None = Header(
        None,
        alias="Idempotency-Key",
        description=(
            "客户端幂等键；仅持久化调用可用。同规则集同键的相同请求复用首次"
            "结果与记录 ID（即使并发重试或期间发布新版本）；文档或版本选择"
            "方式不同返回 409；求值失败不占用键"
        ),
    ),
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """单文档决策。

    携带 ``Idempotency-Key`` 时（仅持久化调用允许）：首次受理即固定实际
    规则版本，将规则集、键、请求的版本选择方式、按对象键排序规范化的文档
    及完整决策结果关联保存；之后同规则集同键的相同请求即使并发重试或期间
    出现新版本，也返回首次结果及原记录 ID，不重复求值或落库。键相同但
    文档或版本选择方式不同返回 409；求值失败（如 NOT_CONVERGED）不占用键。
    未携带键的调用保持原有行为。
    """
    row = crud.get_ruleset(conn, ruleset_id)
    if row is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)

    # 幂等键只对持久化调用有意义；非持久化试算携带键明确拒绝。
    # 纯空白键经 HTTP 传输规范化后等同未携带，按无键处理。
    key = idempotency_key.strip() if idempotency_key is not None else None
    if not key:
        key = None
    if key is not None:
        if not persist:
            return _error(
                "IDEMPOTENCY_KEY_REQUIRES_PERSIST",
                "非持久化调用（persist=false）不支持幂等键",
                400,
            )
        if len(key) > 255:
            return _error(
                "INVALID_IDEMPOTENCY_KEY",
                "幂等键长度不能超过 255 个字符",
                400,
            )

    version_mode = (
        crud.VERSION_MODE_PINNED if version is not None else crud.VERSION_MODE_LATEST
    )

    def _resolve_version(db: sqlite3.Connection) -> int:
        if version is not None:
            return version
        return crud.get_ruleset(db, ruleset_id)["current_version"]

    def _evaluator(
        db: sqlite3.Connection, target_version: int
    ) -> dict[str, Any]:
        _snapshot, compiled = _load_compiled(db, ruleset_id, target_version)
        return evaluate(compiled, req.document)

    if key is None:
        target_version = version or row["current_version"]
        try:
            _snapshot, compiled = _load_compiled(conn, ruleset_id, target_version)
        except LookupError:
            return _error(
                "VERSION_NOT_FOUND", f"版本 {target_version} 不存在", 404
            )
        result = evaluate(compiled, req.document)
        return _persist_evaluation(
            conn, ruleset_id, target_version, req.document, result, persist
        )

    try:
        record, _replayed = crud.evaluate_idempotent(
            conn,
            ruleset_id,
            key,
            version_mode,
            version,
            _resolve_version,
            req.document,
            _evaluator,
        )
    except LookupError:
        # 事务已在 crud 内回滚；latest 模式不会发生（当前版本必然存在），
        # pinned 模式下保留原有 VERSION_NOT_FOUND 语义。
        return _error(
            "VERSION_NOT_FOUND", f"版本 {version} 不存在", 404
        )
    except crud.IdempotencyConflict as exc:
        return _error(exc.code, exc.message, 409, details=exc.details)
    return record


@app.post(
    "/api/rulesets/{ruleset_id}/evaluate-batch",
    response_model=BatchEvaluationOut,
    tags=["evaluation"],
    responses={404: {"model": ErrorResponse}},
)
def evaluate_batch(
    ruleset_id: str,
    req: EvaluateBatchRequest,
    persist: bool = Query(True, description="是否持久化各项成功的结果"),
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """批量评估：整批固定使用同一规则版本，逐项独立求值。

    版本/规则集级别的问题（不存在）返回 404；单项求值失败只记录在该项的
    error 字段中，不中止整批。
    """
    row = crud.get_ruleset(conn, ruleset_id)
    if row is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)

    # 整批只解析、编译一次，所有项固定使用同一版本
    target_version = req.version or row["current_version"]
    try:
        _snapshot, compiled = _load_compiled(conn, ruleset_id, target_version)
    except LookupError:
        return _error("VERSION_NOT_FOUND", f"版本 {target_version} 不存在", 404)

    results: list[dict[str, Any]] = []
    success_count = 0
    error_count = 0

    for index, document in enumerate(req.documents):
        try:
            result = evaluate(compiled, document)
            record = _persist_evaluation(
                conn, ruleset_id, target_version, document, result, persist
            )
        except EvaluationFailed as exc:
            error_count += 1
            results.append(
                {
                    "index": index,
                    "status": "error",
                    "result": None,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    },
                }
            )
        except Exception as exc:  # 单项失败隔离，不中止整批
            error_count += 1
            results.append(
                {
                    "index": index,
                    "status": "error",
                    "result": None,
                    "error": {
                        "code": "EVALUATION_ERROR",
                        "message": f"文档求值失败：{exc}",
                        "details": {},
                    },
                }
            )
        else:
            success_count += 1
            results.append(
                {"index": index, "status": "success", "result": record, "error": None}
            )

    return {
        "ruleset_id": ruleset_id,
        "version": target_version,
        "total": len(req.documents),
        "success_count": success_count,
        "error_count": error_count,
        "results": results,
    }


@app.post(
    "/api/rulesets/{ruleset_id}/coverage",
    response_model=CoverageOut,
    tags=["evaluation"],
    responses={404: {"model": ErrorResponse}},
)
def ruleset_coverage(
    ruleset_id: str,
    req: CoverageRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """多文档覆盖分析：逐项无落库求值并隔离失败，汇总覆盖与贪心代表输入。

    整批固定使用请求指定的不可变 ``version``（只解析编译一次），每份文档
    独立求值且**不写入 evaluations**。单项失败只记录在该项的 error 中（按
    原下标返回），不产生覆盖单元、不参与代表选择，也不中止其余项。

    以每份成功结果的 ``fired_rule_codes`` 与 ``conflicts`` 中的
    “组/字段 + 胜出方 + 被压制规则”构造覆盖单元，汇总：全部规则的命中
    次数、从未命中的规则、去重后的已观察冲突场景；再按“每轮新增覆盖单元
    最多、并列取最小输入下标”的确定性贪心选出 representative_indexes，
    直至无新增覆盖。规则集/版本不存在分别返回 404。
    """
    row = crud.get_ruleset(conn, ruleset_id)
    if row is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)

    try:
        _snapshot, compiled = _load_compiled(conn, ruleset_id, req.version)
    except LookupError:
        return _error("VERSION_NOT_FOUND", f"版本 {req.version} 不存在", 404)

    outcomes: list[dict[str, Any]] = []
    for index, document in enumerate(req.documents):
        try:
            result = evaluate(compiled, document)
        except EvaluationFailed as exc:
            outcomes.append(
                {
                    "index": index,
                    "status": "error",
                    "result": None,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    },
                }
            )
        except Exception as exc:  # 单项失败隔离，不中止整批且不落库
            outcomes.append(
                {
                    "index": index,
                    "status": "error",
                    "result": None,
                    "error": {
                        "code": "EVALUATION_ERROR",
                        "message": f"文档求值失败：{exc}",
                        "details": {},
                    },
                }
            )
        else:
            outcomes.append({"index": index, "status": "success", "result": result})

    summary = build_coverage(compiled.evaluation_order, outcomes)
    return {"ruleset_id": ruleset_id, "version": req.version, **summary}


@app.post(
    "/api/rulesets/{ruleset_id}/compare",
    response_model=CompareOut,
    tags=["evaluation"],
    responses={404: {"model": ErrorResponse}},
)
def compare_versions(
    ruleset_id: str,
    req: CompareRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """发布前预演：同一份文档分别按两个版本求值（不落库），归并差异。

    规则集或任一版本不存在属于请求级错误，返回 404；两侧求值互相隔离，
    一侧失败只在该侧 error 中按现有错误结构返回，不影响另一侧，且
    summary / decision_diff 为 null。
    """
    row = crud.get_ruleset(conn, ruleset_id)
    if row is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)

    try:
        _base_snapshot, base_compiled = _load_compiled(
            conn, ruleset_id, req.base_version
        )
    except LookupError:
        return _error(
            "VERSION_NOT_FOUND", f"版本 {req.base_version} 不存在", 404
        )
    try:
        _target_snapshot, target_compiled = _load_compiled(
            conn, ruleset_id, req.target_version
        )
    except LookupError:
        return _error(
            "VERSION_NOT_FOUND", f"版本 {req.target_version} 不存在", 404
        )

    def _run_side(version: int, compiled: CompiledRuleset) -> dict[str, Any]:
        try:
            result = evaluate(compiled, req.document)
        except EvaluationFailed as exc:
            return {
                "version": version,
                "status": "error",
                "result": None,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            }
        except Exception as exc:  # 单侧失败隔离，不影响另一侧
            return {
                "version": version,
                "status": "error",
                "result": None,
                "error": {
                    "code": "EVALUATION_ERROR",
                    "message": f"文档求值失败：{exc}",
                    "details": {},
                },
            }
        # persist=False：只算不存，记录结构与 evaluate?persist=false 同构
        record = _persist_evaluation(
            conn, ruleset_id, version, req.document, result, persist=False
        )
        return {"version": version, "status": "success", "result": record, "error": None}

    base_side = _run_side(req.base_version, base_compiled)
    target_side = _run_side(req.target_version, target_compiled)

    summary: dict[str, Any] | None = None
    diff: dict[str, Any] | None = None
    if base_side["status"] == "success" and target_side["status"] == "success":
        base_result = base_side["result"]
        target_result = target_side["result"]
        summary = compare_results(base_result, target_result)
        diff = decision_diff(base_result["decision"], target_result["decision"])

    return {
        "ruleset_id": ruleset_id,
        "base_version": req.base_version,
        "target_version": req.target_version,
        "base": base_side,
        "target": target_side,
        "summary": summary,
        "decision_diff": diff,
    }


@app.post(
    "/api/rulesets/{ruleset_id}/recompute",
    response_model=EvaluationOut,
    tags=["evaluation"],
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def recompute(
    ruleset_id: str,
    req: RecomputeRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """用最新版本重算。文档取请求体；为空则重放该规则集最近一次决策的文档。"""
    row = crud.get_ruleset(conn, ruleset_id)
    if row is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)

    document = req.document
    if document is None:
        document = crud.latest_evaluation_document(conn, ruleset_id)
        if document is None:
            return _error(
                "NO_DOCUMENT_TO_RECOMPUTE",
                "请求体未提供 document，且该规则集没有历史决策文档可重放",
                400,
            )

    target_version = row["current_version"]
    _snapshot, compiled = _load_compiled(conn, ruleset_id, target_version)
    result = evaluate(compiled, document)
    # 重算产生新记录，旧结果依旧保留
    return crud.save_evaluation(conn, ruleset_id, target_version, document, result)


@app.get(
    "/api/rulesets/{ruleset_id}/evaluations",
    tags=["evaluation"],
    responses={404: {"model": ErrorResponse}},
)
def list_evaluations(
    ruleset_id: str,
    limit: int = Query(100, ge=1, le=500),
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    if crud.get_ruleset(conn, ruleset_id) is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    return crud.list_evaluations(conn, ruleset_id, limit=limit)


@app.get(
    "/api/rulesets/{ruleset_id}/evaluation-stats",
    response_model=EvaluationStatsOut,
    tags=["evaluation"],
    responses={404: {"model": ErrorResponse}},
)
def evaluation_stats(
    ruleset_id: str,
    version: int | None = Query(None, ge=1, description="只统计指定版本"),
    since: datetime | None = Query(
        None, description="起始时间（ISO-8601，含边界；无时区按 UTC 处理）"
    ),
    until: datetime | None = Query(
        None, description="截止时间（ISO-8601，含边界；无时区按 UTC 处理）"
    ),
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """聚合该规则集已持久化的决策结果（persist=false 的试算不计入）。"""
    if crud.get_ruleset(conn, ruleset_id) is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    return crud.evaluation_stats(
        conn,
        ruleset_id,
        version=version,
        since=_iso_utc_bound(since),
        until=_iso_utc_bound(until),
    )


@app.get(
    "/api/evaluations/{evaluation_id}",
    response_model=EvaluationOut,
    tags=["evaluation"],
    responses={404: {"model": ErrorResponse}},
)
def get_evaluation(
    evaluation_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> Any:
    record = crud.get_evaluation(conn, evaluation_id)
    if record is None:
        return _error("NOT_FOUND", f"决策结果不存在：{evaluation_id}", 404)
    return record
