"""持久化层：规则集、不可变版本快照、决策结果、快照迁移。"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .engine import compile_ruleset

ID_PATTERN_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"

# 快照包格式
SNAPSHOT_FORMAT = "ruleset-snapshot"
SNAPSHOT_FORMAT_VERSION = 1
SNAPSHOT_DIGEST_ALGORITHM = "sha256"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def valid_custom_id(value: str) -> bool:
    return bool(value) and all(c in ID_PATTERN_CHARS for c in value)


class InvalidCustomId(ValueError):
    """自定义规则集 id 非法。"""


# ---------- 幂等键 ----------


VERSION_MODE_LATEST = "latest"
VERSION_MODE_PINNED = "pinned"


class IdempotencyConflict(ValueError):
    """同一 (规则集, 幂等键) 再次出现，但文档或版本选择方式与首次不一致。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def canonical_json(value: Any) -> str:
    """递归按对象键排序的规范化 JSON（紧凑、UTF-8、不转义非 ASCII）。

    与快照摘要一致：sort_keys 保证不同键序的等价文档得到相同串，
    数组按下标保序（顺序即语义）。
    """
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def document_fingerprint(document: Any) -> tuple[str, str]:
    """返回 (规范化文档 JSON, 其 sha256)。"""
    canonical = canonical_json(document)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, digest


def request_fingerprint(
    version_mode: str, pin: int | None, doc_hash: str
) -> str:
    """版本选择方式 + 指定版本（latest 为空）+ 文档哈希的复合指纹。

    latest 模式不包含解析出的实际版本号——因此“首次用最新版、之后发布了
    新版本再重试”仍与首次选择方式一致，应回放首次结果。
    """
    return f"{version_mode}:{pin if pin is not None else ''}:{doc_hash}"


def get_idempotency(
    conn: sqlite3.Connection, ruleset_id: str, key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM idempotency_keys WHERE ruleset_id = ? AND key = ?",
        (ruleset_id, key),
    ).fetchone()


def evaluate_idempotent(
    conn: sqlite3.Connection,
    ruleset_id: str,
    key: str,
    version_mode: str,
    pin: int | None,
    resolve_version,
    document: Any,
    evaluator,
):
    """幂等求值：命中已存键直接返回首次记录；否则在写锁内求值并原子落库。

    - ``pin``：pinned 模式时客户端指定的版本号；latest 模式为 ``None``；
    - ``resolve_version(conn)``：在事务内解析实际版本（latest 取当前版本，
      pinned 取指定版本），版本不存在抛 ``LookupError``；
    - ``evaluator(conn, target_version)``：编译对应版本并对文档求值，
      返回 result；求值失败允许抛出（此时不占键，事务整体回滚）。

    并发安全：先做一次无锁快查命中；未命中则 ``BEGIN IMMEDIATE`` 取写锁后
    复查，保证同键并发请求只有一个真正求值落库，其余复用首次结果。

    返回 (record, replayed)：replayed=True 表示复用了首次结果。
    同键但文档或版本选择方式（模式 / 指定版本）不同抛
    :class:`IdempotencyConflict`。
    """
    canonical, doc_hash = document_fingerprint(document)

    # 无锁快查：命中直接复用，避免重复求值
    existing = get_idempotency(conn, ruleset_id, key)
    if existing is not None:
        return _replay_or_conflict(existing, version_mode, pin, doc_hash, conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 取写锁后复查：并发请求可能已在此期间完成首次受理
        existing = get_idempotency(conn, ruleset_id, key)
        if existing is not None:
            record, _replayed = _replay_or_conflict(
                existing, version_mode, pin, doc_hash, conn
            )
            return record, True

        target_version = resolve_version(conn)
        result = evaluator(conn, target_version)
        record = save_evaluation(conn, ruleset_id, target_version, document, result)
        fingerprint = request_fingerprint(version_mode, pin, doc_hash)
        conn.execute(
            """INSERT INTO idempotency_keys
               (ruleset_id, key, version_mode, pinned_version,
                document_hash, document_canonical, fingerprint,
                evaluation_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ruleset_id,
                key,
                version_mode,
                pin,
                doc_hash,
                canonical,
                fingerprint,
                record["id"],
                record["created_at"],
            ),
        )
        return record, False
    except Exception:
        conn.rollback()
        raise


def _replay_or_conflict(
    existing: sqlite3.Row,
    version_mode: str,
    pin: int | None,
    doc_hash: str,
    conn: sqlite3.Connection,
):
    """已存键：版本选择方式（模式/指定版本）或文档不同 → 409；否则返回首次记录。"""
    if (
        existing["version_mode"] != version_mode
        or existing["pinned_version"] != pin
    ):
        raise IdempotencyConflict(
            "IDEMPOTENCY_KEY_CONFLICT",
            "幂等键已用于不同的版本选择方式",
            {
                "idempotency_key": existing["key"],
                "field": "version_selection",
                "original": {
                    "mode": existing["version_mode"],
                    "version": existing["pinned_version"],
                },
                "conflicting": {"mode": version_mode, "version": pin},
            },
        )
    if existing["document_hash"] != doc_hash:
        raise IdempotencyConflict(
            "IDEMPOTENCY_KEY_CONFLICT",
            "幂等键已用于不同的请求文档",
            {
                "idempotency_key": existing["key"],
                "field": "document",
            },
        )
    record = get_evaluation(conn, existing["evaluation_id"])
    if record is None:
        # 理论上不会发生（外键约束 + 同事务落库）；防御性兜底
        raise IdempotencyConflict(
            "IDEMPOTENCY_RECORD_MISSING",
            "幂等键关联的首次决策记录不存在",
            {"idempotency_key": existing["key"]},
        )
    return record, True


# ---------- 序列化 ----------


def ruleset_payload(payload_in: dict[str, Any]) -> dict[str, Any]:
    """规范化创建/更新请求体。"""
    return {
        "name": payload_in["name"],
        "description": payload_in.get("description") or "",
        "default_conflict_policy": payload_in.get(
            "default_conflict_policy", "collect_all"
        ),
        "rules": [
            {
                "code": r["code"],
                "name": r.get("name") or r["code"],
                "description": r.get("description") or "",
                "priority": r.get("priority", 0),
                "conflict_group": r.get("conflict_group"),
                "conflict_policy": r.get("conflict_policy"),
                "depends_on": list(r.get("depends_on") or []),
                "condition": r.get("condition") or "",
                "action": r.get("action"),
            }
            for r in payload_in["rules"]
        ],
    }


def snapshot_from_compiled(compiled, version: int) -> dict[str, Any]:
    return {
        "version": version,
        "name": compiled.name,
        "description": compiled.description,
        "default_conflict_policy": compiled.default_conflict_policy,
        "evaluation_order": compiled.evaluation_order,
        "rules": [
            {
                "code": r.code,
                "name": r.name,
                "description": r.description,
                "priority": r.priority,
                "conflict_group": r.conflict_group,
                "conflict_policy": r.conflict_policy,
                "depends_on": list(r.depends_on),
                "condition": r.condition,
                "action": r.action or None,
                "referenced_rules": sorted(r.referenced_rules),
            }
            for r in sorted(compiled.rules, key=lambda x: x.order_index)
        ],
    }


# ---------- 规则集 ----------


def create_ruleset(conn: sqlite3.Connection, payload_in: dict[str, Any]) -> tuple[str, int, dict]:
    """编译 → 写规则集与 v1 快照。返回 (id, version, snapshot)。"""
    custom_id = payload_in.get("id")
    if isinstance(custom_id, str) and custom_id.strip() == "":
        raise InvalidCustomId("自定义 id 不能为空字符串")
    if custom_id is not None and not valid_custom_id(custom_id):
        raise InvalidCustomId("自定义 id 只能包含字母、数字、-、_")
    if custom_id is not None and get_ruleset(conn, custom_id) is not None:
        raise KeyError(f"规则集 id 已存在：{custom_id}")

    payload = ruleset_payload(payload_in)
    compiled = compile_ruleset(
        payload["name"],
        payload["description"],
        payload["default_conflict_policy"],
        payload["rules"],
    )

    ruleset_id = custom_id if custom_id is not None else new_id()

    ts = now()
    version = 1
    snapshot = snapshot_from_compiled(compiled, version)
    version_id = new_id()

    conn.execute(
        """INSERT INTO rulesets
           (id, name, description, default_conflict_policy,
            current_version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            ruleset_id,
            compiled.name,
            compiled.description,
            compiled.default_conflict_policy,
            version,
            ts,
            ts,
        ),
    )
    conn.execute(
        """INSERT INTO ruleset_versions
           (id, ruleset_id, version, snapshot, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (version_id, ruleset_id, version, json.dumps(snapshot, ensure_ascii=False), ts),
    )
    return ruleset_id, version, snapshot


def update_ruleset(conn: sqlite3.Connection, ruleset_id: str, payload_in: dict[str, Any]) -> int:
    """修改规则集：编译新版本快照并更新 current_version。旧版本保留。"""
    existing = get_ruleset(conn, ruleset_id)
    if existing is None:
        raise LookupError(f"规则集不存在：{ruleset_id}")

    payload = ruleset_payload(payload_in)
    compiled = compile_ruleset(
        payload["name"],
        payload["description"],
        payload["default_conflict_policy"],
        payload["rules"],
    )

    new_version = existing["current_version"] + 1
    snapshot = snapshot_from_compiled(compiled, new_version)
    ts = now()
    conn.execute(
        """INSERT INTO ruleset_versions
           (id, ruleset_id, version, snapshot, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (new_id(), ruleset_id, new_version, json.dumps(snapshot, ensure_ascii=False), ts),
    )
    conn.execute(
        """UPDATE rulesets
           SET name=?, description=?, default_conflict_policy=?,
               current_version=?, updated_at=?
           WHERE id=?""",
        (
            compiled.name,
            compiled.description,
            compiled.default_conflict_policy,
            new_version,
            ts,
            ruleset_id,
        ),
    )
    return new_version


def get_ruleset(conn: sqlite3.Connection, ruleset_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM rulesets WHERE id = ?", (ruleset_id,)
    ).fetchone()


def list_rulesets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM rulesets ORDER BY created_at, id").fetchall()
    )


def list_versions(conn: sqlite3.Connection, ruleset_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT id, ruleset_id, version, created_at
               FROM ruleset_versions
               WHERE ruleset_id = ?
               ORDER BY version""",
            (ruleset_id,),
        ).fetchall()
    )


def get_version(
    conn: sqlite3.Connection, ruleset_id: str, version: int
) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT id, ruleset_id, version, snapshot, created_at
           FROM ruleset_versions
           WHERE ruleset_id = ? AND version = ?""",
        (ruleset_id, version),
    ).fetchone()
    if row is None:
        return None
    snapshot = json.loads(row["snapshot"])
    return {
        "id": row["id"],
        "ruleset_id": row["ruleset_id"],
        "version": row["version"],
        "created_at": row["created_at"],
        **snapshot,
    }


def latest_version_number(conn: sqlite3.Connection, ruleset_id: str) -> int | None:
    row = conn.execute(
        "SELECT current_version FROM rulesets WHERE id = ?", (ruleset_id,)
    ).fetchone()
    return None if row is None else row["current_version"]


# ---------- 规则集快照迁移（导出 / 导入） ----------


class SnapshotPackageError(ValueError):
    """快照包不符合迁移格式或摘要核对失败；不写库。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def canonical_ruleset_content(ruleset_in: dict[str, Any]) -> dict[str, Any]:
    """把规则集定义规范化为稳定结构（供摘要与重编译共用）。

    仅包含参与规则语义的字段：name / description /
    default_conflict_policy / rules；规则按提交顺序保留，depends_on
    去重保序。与创建/更新接口走同一规范化逻辑，保证“同一内容无论来源
    （创建、更新、导入、已存储快照）”摘要一致。
    """
    return ruleset_payload(ruleset_in)


def content_digest(content: dict[str, Any]) -> str:
    """规范化 JSON（sort_keys、无多余空白、UTF-8、不转义非 ASCII）的 sha256。"""
    canonical = json.dumps(
        content, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def snapshot_content(snapshot: dict[str, Any]) -> dict[str, Any]:
    """从已存储版本快照中抽出参与摘要的规则集定义。"""
    return {
        "name": snapshot["name"],
        "description": snapshot.get("description", ""),
        "default_conflict_policy": snapshot["default_conflict_policy"],
        "rules": [
            {
                "code": r["code"],
                "name": r.get("name", r["code"]),
                "description": r.get("description", ""),
                "priority": r.get("priority", 0),
                "conflict_group": r.get("conflict_group"),
                "conflict_policy": r.get("conflict_policy"),
                "depends_on": list(r.get("depends_on") or []),
                "condition": r.get("condition", ""),
                "action": r.get("action"),
            }
            for r in snapshot["rules"]
        ],
    }


def build_snapshot_package(
    snapshot: dict[str, Any], source_ruleset_id: str
) -> dict[str, Any]:
    """把指定版本快照打包为含源标识、完整定义与内容摘要的 JSON 包。"""
    content = canonical_ruleset_content(snapshot_content(snapshot))
    return {
        "format": SNAPSHOT_FORMAT,
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "source": {
            "ruleset_id": source_ruleset_id,
            "version": snapshot["version"],
        },
        "exported_at": now(),
        "digest": {
            "algorithm": SNAPSHOT_DIGEST_ALGORITHM,
            "value": content_digest(content),
        },
        "ruleset": {
            "name": content["name"],
            "description": content["description"],
            "default_conflict_policy": content["default_conflict_policy"],
            "rules": content["rules"],
        },
    }


def import_snapshot(
    conn: sqlite3.Connection, target_ruleset_id: str, package: dict[str, Any]
) -> tuple[str, int, dict[str, Any]]:
    """核对摘要 → 重编译 → 幂等导入快照包。

    返回 (action, version, snapshot)，action 为
    created（目标不存在，创建 v1）/ matched（已有内容一致版本）/
    appended（追加新版本）。任何校验失败都抛异常，且不写库。

    版本号始终按目标规则集递增（current_version+1，新建为 1），
    不采用包内 source.version。整个过程在单连接事务内完成，由上层
    get_db 统一提交/回滚，保证原子化。
    """
    # 1) 包格式校验（Pydantic 已保证字段存在，这里做防御性兜底）
    if package.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotPackageError(
            "INVALID_SNAPSHOT_FORMAT",
            f"快照包 format 必须为 {SNAPSHOT_FORMAT!r}",
            {"format": package.get("format")},
        )
    if package.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise SnapshotPackageError(
            "UNSUPPORTED_SNAPSHOT_VERSION",
            f"不支持的快照包格式版本：{package.get('format_version')!r}",
            {"format_version": package.get("format_version")},
        )

    digest_info = package.get("digest") or {}
    algorithm = digest_info.get("algorithm")
    provided = digest_info.get("value")
    if algorithm != SNAPSHOT_DIGEST_ALGORITHM:
        raise SnapshotPackageError(
            "UNSUPPORTED_DIGEST_ALGORITHM",
            f"仅支持 {SNAPSHOT_DIGEST_ALGORITHM} 摘要，收到：{algorithm!r}",
            {"algorithm": algorithm},
        )
    if not isinstance(provided, str) or not provided.strip():
        raise SnapshotPackageError(
            "INVALID_SNAPSHOT_DIGEST", "快照包缺少内容摘要 digest.value"
        )

    ruleset_body = package.get("ruleset")
    if not isinstance(ruleset_body, dict):
        raise SnapshotPackageError(
            "INVALID_SNAPSHOT_RULESET", "快照包缺少 ruleset 规则集定义"
        )

    # 2) 规范化内容并核对摘要；不符则拒绝（防篡改/传输损坏）
    content = canonical_ruleset_content(ruleset_body)
    actual = content_digest(content)
    if not hmac.compare_digest(actual, provided.strip().lower()):
        raise SnapshotPackageError(
            "DIGEST_MISMATCH",
            "快照内容摘要核对失败：包内规则定义与摘要不一致，已拒绝导入",
            {"expected": provided, "actual": actual},
        )

    # 3) 重新编译（全部静态校验：重复编码、未知依赖、循环依赖、策略冲突等）
    compiled = compile_ruleset(
        content["name"],
        content["description"],
        content["default_conflict_policy"],
        content["rules"],
    )

    # 4) 加写锁后原子决策：复核目标存在性与全部历史版本的内容摘要，
    #    杜绝“检查—写入”之间被并发提交插空。整个写入在单事务内完成，
    #    由上层 get_db 统一提交/回滚。
    conn.execute("BEGIN IMMEDIATE")
    existing = get_ruleset(conn, target_ruleset_id)
    if existing is not None:
        rows = conn.execute(
            "SELECT version, snapshot FROM ruleset_versions WHERE ruleset_id = ?",
            (target_ruleset_id,),
        ).fetchall()
        for row in rows:
            stored = json.loads(row["snapshot"])
            if content_digest(canonical_ruleset_content(snapshot_content(stored))) == actual:
                matched = get_version(conn, target_ruleset_id, row["version"])
                return "matched", row["version"], matched

    ts = now()
    if existing is None:
        # 目标 ID 不存在 → 创建 v1
        version = 1
        snapshot = snapshot_from_compiled(compiled, version)
        conn.execute(
            """INSERT INTO rulesets
               (id, name, description, default_conflict_policy,
                current_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                target_ruleset_id,
                compiled.name,
                compiled.description,
                compiled.default_conflict_policy,
                version,
                ts,
                ts,
            ),
        )
        conn.execute(
            """INSERT INTO ruleset_versions
               (id, ruleset_id, version, snapshot, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (new_id(), target_ruleset_id, version,
             json.dumps(snapshot, ensure_ascii=False), ts),
        )
        action = "created"
    else:
        # 内容与任一版本都不一致 → 按目标规则集 current_version+1 追加
        version = existing["current_version"] + 1
        snapshot = snapshot_from_compiled(compiled, version)
        conn.execute(
            """INSERT INTO ruleset_versions
               (id, ruleset_id, version, snapshot, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (new_id(), target_ruleset_id, version,
             json.dumps(snapshot, ensure_ascii=False), ts),
        )
        conn.execute(
            """UPDATE rulesets
               SET name=?, description=?, default_conflict_policy=?,
                   current_version=?, updated_at=?
               WHERE id=?""",
            (
                compiled.name,
                compiled.description,
                compiled.default_conflict_policy,
                version,
                ts,
                target_ruleset_id,
            ),
        )
        action = "appended"

    stored = get_version(conn, target_ruleset_id, version)
    return action, version, stored


# ---------- 决策结果 ----------


def save_evaluation(
    conn: sqlite3.Connection,
    ruleset_id: str,
    version: int,
    document: Any,
    result: dict[str, Any],
) -> dict[str, Any]:
    eval_id = new_id()
    ts = now()
    conn.execute(
        """INSERT INTO evaluations
           (id, ruleset_id, version, document, result,
            converged, iterations, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            eval_id,
            ruleset_id,
            version,
            json.dumps(document, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
            1 if result.get("converged") else 0,
            result.get("iterations", 0),
            ts,
        ),
    )
    return _evaluation_record(eval_id, ruleset_id, version, document, result, ts)


def _evaluation_record(
    eval_id: str,
    ruleset_id: str,
    version: int,
    document: Any,
    result: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    return {
        "id": eval_id,
        "ruleset_id": ruleset_id,
        "version": version,
        "document": document,
        "created_at": created_at,
        **{k: v for k, v in result.items()},
    }


def get_evaluation(conn: sqlite3.Connection, eval_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM evaluations WHERE id = ?", (eval_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_evaluation(row)


def list_evaluations(
    conn: sqlite3.Connection, ruleset_id: str, limit: int = 100
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM evaluations
           WHERE ruleset_id = ?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (ruleset_id, limit),
    ).fetchall()
    return [_row_to_evaluation(r) for r in rows]


def latest_evaluation_document(
    conn: sqlite3.Connection, ruleset_id: str
) -> Any | None:
    row = conn.execute(
        """SELECT document FROM evaluations
           WHERE ruleset_id = ?
           ORDER BY created_at DESC, id DESC
           LIMIT 1""",
        (ruleset_id,),
    ).fetchone()
    return None if row is None else json.loads(row["document"])


def evaluation_stats(
    conn: sqlite3.Connection,
    ruleset_id: str,
    version: int | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """聚合已持久化决策：总数、规则命中（取自 traces）与决策字段出现次数。

    since/until 须为与 created_at 同格式的 UTC 秒级字符串（含边界，字典序
    即时间序）；结果按规则编码 / 字段名排序，无匹配记录时返回零值结构。
    """
    clauses = ["ruleset_id = ?"]
    params: list[Any] = [ruleset_id]
    if version is not None:
        clauses.append("version = ?")
        params.append(version)
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("created_at <= ?")
        params.append(until)
    rows = conn.execute(
        f"SELECT result FROM evaluations WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()

    total = 0
    hit_counts: dict[str, int] = {}
    field_counts: dict[str, int] = {}
    for row in rows:
        result = json.loads(row["result"])
        total += 1
        for trace in result.get("traces") or []:
            code = trace.get("rule_code")
            if not code:
                continue
            hit_counts.setdefault(code, 0)
            if trace.get("hit"):
                hit_counts[code] += 1
        decision = result.get("decision")
        if isinstance(decision, dict):
            for field in decision:
                field_counts[field] = field_counts.get(field, 0) + 1

    return {
        "ruleset_id": ruleset_id,
        "version": version,
        "since": since,
        "until": until,
        "total_evaluations": total,
        "rule_stats": [
            {
                "code": code,
                "hit_count": count,
                "hit_rate": round(count / total, 4) if total else 0.0,
            }
            for code, count in sorted(hit_counts.items())
        ],
        "decision_field_counts": [
            {"field": field, "count": count}
            for field, count in sorted(field_counts.items())
        ],
    }


def _row_to_evaluation(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["result"])
    document = json.loads(row["document"])
    return {
        "id": row["id"],
        "ruleset_id": row["ruleset_id"],
        "version": row["version"],
        "document": document,
        "created_at": row["created_at"],
        **result,
    }
