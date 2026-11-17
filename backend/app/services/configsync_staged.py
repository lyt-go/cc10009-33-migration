"""分阶段同步结果数据集与阶段应用规则。

分阶段同步结果由三块拼成，任何一块都不覆盖旧数据：

1. ``阶段``：从旧载荷转换出来的段记录，逐项带应用状态、确认状态和幂等标识；
2. ``版本依据``：旧审计、节点版本记录两路来源，同时存在时如实列出两组；
3. ``冲突``：映射不上或会让节点版本倒退的情况，保留为待处理记录而不是丢掉。

阶段应用/确认都按幂等标识判重：同一阶段重复提交只返回第一次的结果，不再写
节点版本、不再写审计，也不产生第二条分阶段同步结果。
"""
from __future__ import annotations

from typing import Any

from app.services.configsync_versions import (
    active_node_version,
    compare_versions,
    node_version_rows,
    now_text,
    payload_checksum,
    retire_older_node_versions,
)
from app.store import store

STAGED_MODULE = "configsync_staged"
CONFLICT_MODULE = "configsync_conflicts"
AUDIT_MODULE = "configsync_audit"
NODE_VERSION_MODULE = "node_config_versions"

APPLIED = "已应用"
NOT_APPLIED = "未应用"
CONFIRMED = "已确认"
NOT_CONFIRMED = "未确认"


def staged_rows_for_task(task_id: int) -> list[dict[str, Any]]:
    return sorted(
        (
            row
            for row in store.rows(STAGED_MODULE)
            if int(row.get("任务ID", 0) or 0) == task_id
        ),
        key=lambda row: int(row.get("阶段序号", 0) or 0),
    )


def conflicts_for_task(task_id: int) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows(CONFLICT_MODULE)
        if int(row.get("任务ID", 0) or 0) == task_id
    ]


def _audit_rows(task_id: int) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows(AUDIT_MODULE)
        if int(row.get("任务ID", 0) or 0) == task_id
    ]


def _node_version_exists(node: str, version: str) -> dict[str, Any] | None:
    target = str(version or "").strip()
    for row in node_version_rows(node):
        if str(row.get("配置版本号") or "").strip() == target:
            return row
    return None


def version_evidence(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """分阶段同步结果里的版本依据。

    旧审计里"确认完成"记录的版本是一组依据，节点配置版本表里的记录是另一组；
    两者不一致时两组都列出，供逐项复核，不替任何一方"修正"。
    """
    task_id = int(entry.get("id", 0) or 0)
    node = str(entry.get("目标节点") or "").strip()
    version = str(entry.get("配置版本号") or "").strip()
    evidence: list[dict[str, Any]] = []

    version_record = _node_version_exists(node, version)
    confirms = [row for row in _audit_rows(task_id) if row.get("动作") == "确认完成"]
    if confirms:
        latest = confirms[-1]
        evidence.append(
            {
                "依据类型": "旧审计",
                "来源": f"审计#{latest.get('id')}",
                "配置版本号": version,
                "可读": True,
                "完整": bool(version_record and version_record.get("完整")),
                "时间": latest.get("时间"),
                "节点已落库": version_record is not None,
                "说明": "旧载荷一次性确认完成时写入的版本依据",
            }
        )

    node_rows = [
        row
        for row in node_version_rows(node)
        if bool(row.get("完整")) and str(row.get("状态") or "") in {"生效", "已替代"}
    ]
    for row in node_rows:
        evidence.append(
            {
                "依据类型": "节点版本记录",
                "来源": f"节点版本#{row.get('id')}",
                "配置版本号": row.get("配置版本号"),
                "可读": True,
                "完整": True,
                "时间": row.get("生效时间"),
                "节点已落库": True,
                "说明": f"来源任务 {row.get('来源任务') or '—'}（{row.get('状态')}）",
            }
        )
    return evidence


def two_version_bases(entry: dict[str, Any]) -> dict[str, Any]:
    """同一版本在旧审计与节点记录里对不上时，返回"两组版本依据"的说明。"""
    evidence = version_evidence(entry)
    version = str(entry.get("配置版本号") or "").strip()
    other = [item for item in evidence if str(item.get("配置版本号") or "") != version]
    confirms = [item for item in evidence if item.get("依据类型") == "旧审计"]
    own_node_row = [
        item
        for item in evidence
        if item.get("依据类型") == "节点版本记录"
        and str(item.get("配置版本号") or "") == version
    ]
    reads = [item for item in evidence if item.get("可读")]
    conflict = bool(confirms) and not own_node_row and len(reads) > 1
    return {
        "存在": conflict,
        "旧载荷版本": version,
        "版本依据": evidence,
        "依据组数": len(reads),
        "冲突节点版本": [item.get("配置版本号") for item in other],
        "说明": (
            "旧载荷已确认完成，但该版本尚未完整写入节点配置版本表；"
            "目标节点同时存在旧审计与节点记录两种可读版本依据"
            if conflict
            else None
        ),
    }


def build_result_dataset(entry: dict[str, Any]) -> dict[str, Any]:
    """一条任务的分阶段同步结果：可逐项复核，只暴露完整应用的节点版本。"""
    task_id = int(entry.get("id", 0) or 0)
    node = str(entry.get("目标节点") or "").strip()
    rows = staged_rows_for_task(task_id)
    versions = two_version_bases(entry)
    active = active_node_version(node)
    # 节点上存在多个完整生效版本时，结果数据集也不能把其中一个当成唯一依据。
    active_count = len(node_readable_versions(node))
    applied_rows = [
        row for row in rows if str(row.get("应用状态") or "") == APPLIED
    ]
    confirmed_rows = [
        row
        for row in rows
        if str(row.get("应用状态") or "") == APPLIED
        and str(row.get("确认状态") or "") == CONFIRMED
    ]
    applied_ids = {int(row.get("id", 0) or 0) for row in applied_rows}
    remaining = [row for row in rows if int(row.get("id", 0) or 0) not in applied_ids]
    unconfirmed = [row for row in applied_rows if row not in confirmed_rows]
    conflicts = conflicts_for_task(task_id)
    pending_conflicts = [row for row in conflicts if row.get("状态") == "待处理"]

    ready = bool(rows) and not remaining and not unconfirmed and not pending_conflicts
    return {
        "任务ID": task_id,
        "任务编号": entry.get("任务编号"),
        "源节点": entry.get("源节点"),
        "目标节点": node,
        "配置版本号": entry.get("配置版本号"),
        "载荷版本": str(entry.get("payload_version") or "legacy-v1"),
        "幂等标识": entry.get("幂等标识"),
        "阶段总数": len(rows),
        "已应用阶段数": len(applied_rows),
        "已确认阶段数": len(confirmed_rows),
        "未应用阶段数": len(remaining),
        "待确认阶段数": len(unconfirmed),
        "阶段": [dict(row) for row in rows],
        "待续做阶段": [
            {
                "阶段序号": row.get("阶段序号"),
                "阶段名": row.get("阶段名"),
                "幂等标识": row.get("应用幂等标识"),
            }
            for row in remaining
        ],
        "待确认阶段": [
            {
                "阶段序号": row.get("阶段序号"),
                "阶段名": row.get("阶段名"),
                "幂等标识": row.get("确认幂等标识"),
            }
            for row in unconfirmed
        ],
        "版本依据": versions["版本依据"],
        "版本依据冲突": versions["存在"] or active_count > 1,
        "版本依据说明": versions["说明"] or (
            f"目标节点同时存在 {active_count} 个完整生效版本，版本依据尚未收敛"
            if active_count > 1
            else None
        ),
        "两组版本依据": versions["存在"] or active_count > 1,
        "节点完整版本数": active_count,
        "冲突节点版本": versions["冲突节点版本"],
        # 结果数据集自带待处理记录，不依赖冲突表是否已落库，便于逐项复核。
        "待处理记录": [
            dict(row)
            for row in versions["版本依据"]
            if row.get("依据类型") == "旧审计" and not row.get("节点已落库")
        ],
        "冲突记录": conflicts,
        "待处理冲突数": len(pending_conflicts),
        "结果摘要": (
            "存在待处理冲突，暂不对外暴露节点版本"
            if pending_conflicts or versions["存在"] or active_count > 1
            else (
                "全部阶段已应用并确认"
                if ready
                else f"待续做 {len(remaining)} 个阶段，待确认 {len(unconfirmed)} 个阶段"
            )
        ),
        "可暴露节点版本": (
            {
                "节点编号": node,
                "配置版本号": active.get("配置版本号"),
                "来源任务": active.get("来源任务"),
                "生效时间": active.get("生效时间"),
                "完整": True,
            }
            if active
            else None
        ),
        "数据集摘要": payload_checksum(
            [
                (row.get("阶段序号"), row.get("幂等标识"), row.get("应用状态"), row.get("确认状态"))
                for row in rows
            ]
        ),
    }


def _find_stage(entry: dict[str, Any], stage_no: int) -> dict[str, Any] | None:
    for row in staged_rows_for_task(int(entry.get("id", 0) or 0)):
        if int(row.get("阶段序号", 0) or 0) == stage_no:
            return row
    return None


def _record_stage_audit(
    *,
    entry: dict[str, Any],
    stage: dict[str, Any],
    action: str,
    previous: str,
    current: str,
    idempotency: str,
    actor: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = store.rows(AUDIT_MODULE)
    record = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "任务ID": entry.get("id"),
        "任务编号": entry.get("任务编号"),
        "动作": action,
        "原状态": previous,
        "新状态": current,
        "载荷版本": str(entry.get("payload_version") or "legacy-v1"),
        "阶段": stage.get("阶段名"),
        "阶段序号": stage.get("阶段序号"),
        "幂等标识": idempotency,
        "操作人": actor,
        "时间": now_text(),
        "详情": detail or {},
    }
    rows.append(record)
    return record


def _node_version_written(entry: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    """写入/刷新节点配置版本记录。

    阶段还没全部应用确认时，记录标成"部分应用、不完整"，工作终端不会读到；
    等全部阶段完成才转成"生效、完整"，并把更低的版本标为已替代。
    """
    node = str(entry.get("目标节点") or "").strip()
    version = str(entry.get("配置版本号") or "").strip()
    existing = _node_version_exists(node, version)
    stamp = now_text()
    if existing is not None:
        # 已有记录只补阶段线索；完整性和生效状态交给 _refresh_node_version 判定。
        applied = {int(item) for item in (existing.get("阶段应用列表") or [])}
        applied.add(int(stage.get("阶段序号", 0) or 0))
        existing["阶段应用列表"] = sorted(applied)
        existing.setdefault("分阶段结果ID", stage.get("id"))
        return existing
    rows = store.rows(NODE_VERSION_MODULE)
    record = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "节点编号": node,
        "配置版本号": version,
        "来源任务": entry.get("任务编号"),
        "来源任务ID": entry.get("id"),
        "分阶段结果ID": stage.get("id"),
        "阶段序号": stage.get("阶段序号"),
        "阶段应用列表": [int(stage.get("阶段序号", 0) or 0)],
        "配置内容哈希": payload_checksum(
            {"任务": entry.get("任务编号"), "版本": version, "阶段": stage.get("阶段名")}
        ),
        "完整": False,
        "状态": "部分应用",
        "生效时间": None,
        "创建时间": stamp,
    }
    rows.append(record)
    return record


def _refresh_node_version(entry: dict[str, Any]) -> dict[str, Any] | None:
    """按阶段进度判定节点版本的完整性；只有全部段应用并确认后才完整生效。"""
    node = str(entry.get("目标节点") or "").strip()
    version = str(entry.get("配置版本号") or "").strip()
    if not node or not version:
        return None
    rows = staged_rows_for_task(int(entry.get("id", 0) or 0))
    record = _node_version_exists(node, version)
    if record is None:
        return None
    all_done = bool(rows) and all(
        str(row.get("应用状态") or "") == APPLIED
        and str(row.get("确认状态") or "") == CONFIRMED
        for row in rows
    )
    if not all_done:
        record["完整"] = False
        if str(record.get("状态") or "") != "生效":
            record["状态"] = "部分应用"
        return record
    if not bool(record.get("完整")):
        record["完整"] = True
        record["状态"] = "生效"
        record["生效时间"] = now_text()
        record["配置内容哈希"] = payload_checksum(
            {"任务": entry.get("任务编号"), "版本": version, "阶段": "全部完成"}
        )
        record["已替代旧版本"] = retire_older_node_versions(node, version)
    return record


def apply_stage(
    entry: dict[str, Any],
    stage_no: int,
    *,
    actor: str = "值班管理员",
    idempotency_key: str | None = None,
) -> tuple[dict[str, Any] | None, str, bool]:
    """应用一个阶段。返回 (阶段记录, 说明, 是否为重复提交)。

    重复提交（阶段已应用，或同一幂等标识已处理过）直接返回既有阶段，不再写
    节点版本与审计；节点版本只会向前走，落后版本一律拒绝。
    """
    stage = _find_stage(entry, stage_no)
    if stage is None:
        return None, f"任务 {entry.get('任务编号')} 没有第 {stage_no} 阶段，请先迁移分阶段载荷", False

    expected_key = str(stage.get("应用幂等标识") or "")
    supplied_key = str(idempotency_key or expected_key).strip()
    if supplied_key != expected_key:
        return None, f"幂等标识不匹配：本阶段应使用 {expected_key}", False

    if str(stage.get("应用状态") or "") == APPLIED:
        return stage, "该阶段已应用，按幂等处理，未重复应用配置", True

    if str(stage.get("阶段状态") or "") == "待处理冲突":
        return None, "该阶段处于版本冲突待处理状态，需先解决冲突再应用", False

    # 分段应用必须按顺序推进：跳过前面未应用的段会让节点版本提前"完整"，
    # 中断后也无法判断落后的是哪一段。
    earlier = [
        row
        for row in staged_rows_for_task(int(entry.get("id", 0) or 0))
        if int(row.get("阶段序号", 0) or 0) < stage_no
        and str(row.get("应用状态") or "") != APPLIED
    ]
    if earlier:
        numbers = [int(row.get("阶段序号", 0) or 0) for row in earlier]
        return None, f"第 {numbers} 阶段尚未应用，分阶段同步只能按顺序续做", False

    node = str(entry.get("目标节点") or "").strip()
    version = str(entry.get("配置版本号") or "").strip()
    active = active_node_version(node)
    if active is not None:
        order = compare_versions(version, active.get("配置版本号"))
        if order < 0:
            message = (
                f"目标节点 {node} 当前生效版本 {active.get('配置版本号')} 高于本任务版本 "
                f"{version}，拒绝回退；已保留待处理记录"
            )
            _add_conflict(entry, "版本回退", stage_no, message, active)
            return None, message, False
        if order == 0:
            # 节点已有同版本：只补充分阶段记录，不再重复写入节点版本。
            previous = str(stage.get("阶段状态") or "")
            stage.update(
                {
                    "应用状态": APPLIED,
                    "阶段状态": "已应用待确认",
                    "应用时间": active.get("生效时间") or now_text(),
                    "来源": "节点版本记录",
                    "pending": True,
                }
            )
            _record_stage_audit(
                entry=entry,
                stage=stage,
                action="应用阶段",
                previous=previous,
                current="已应用待确认",
                idempotency=expected_key,
                actor=actor,
                detail={"节点版本已存在": version, "未重复写入": True},
            )
            _resolve_conflicts(entry, stage_no, "节点已有同版本，阶段记录已补齐")
            return stage, f"节点已存在版本 {version}，阶段记录已补齐，未重复应用", True

    node_version = _node_version_written(entry, stage)
    previous = str(stage.get("阶段状态") or "")
    stage.update(
        {
            "应用状态": APPLIED,
            "阶段状态": "已应用待确认",
            "应用时间": now_text(),
            "来源": "分阶段应用",
            "pending": True,
        }
    )
    refreshed = _refresh_node_version(entry)
    _record_stage_audit(
        entry=entry,
        stage=stage,
        action="应用阶段",
        previous=previous,
        current="已应用待确认",
        idempotency=expected_key,
        actor=actor,
        detail={
            "节点版本": version,
            "节点版本记录ID": node_version.get("id"),
            "节点版本完整": bool(refreshed.get("完整")) if refreshed else None,
            "已替代旧版本": (refreshed or {}).get("已替代旧版本", []),
        },
    )
    _resolve_conflicts(entry, stage_no, "阶段已应用，节点版本已补齐")
    return stage, f"阶段「{stage.get('阶段名')}」已应用，节点版本 {version} 已记录", False


def confirm_stage(
    entry: dict[str, Any],
    stage_no: int,
    *,
    actor: str = "值班管理员",
    idempotency_key: str | None = None,
) -> tuple[dict[str, Any] | None, str, bool]:
    """确认一个阶段；未应用的阶段不允许确认，重复确认按幂等返回。"""
    stage = _find_stage(entry, stage_no)
    if stage is None:
        return None, f"任务 {entry.get('任务编号')} 没有第 {stage_no} 阶段，请先迁移分阶段载荷", False
    expected_key = str(stage.get("确认幂等标识") or "")
    supplied_key = str(idempotency_key or expected_key).strip()
    if supplied_key != expected_key:
        return None, f"幂等标识不匹配：本阶段确认应使用 {expected_key}", False
    if str(stage.get("确认状态") or "") == CONFIRMED:
        return stage, "该阶段已确认，按幂等处理", True
    if str(stage.get("应用状态") or "") != APPLIED:
        return None, "阶段尚未应用，不能确认；请先应用该阶段", False
    previous = str(stage.get("阶段状态") or "")
    stage.update(
        {
            "确认状态": CONFIRMED,
            "阶段状态": "已完成",
            "确认时间": now_text(),
            "pending": False,
        }
    )
    _record_stage_audit(
        entry=entry,
        stage=stage,
        action="确认阶段",
        previous=previous,
        current="已完成",
        idempotency=expected_key,
        actor=actor,
    )
    _refresh_node_version(entry)
    _sync_task_after_stage(entry)
    return stage, f"阶段「{stage.get('阶段名')}」已确认完成", False


def _sync_task_after_stage(entry: dict[str, Any]) -> None:
    """全部阶段确认后把任务推进到已完成；旧字段 status 与 同步状态 同步更新。"""
    rows = staged_rows_for_task(int(entry.get("id", 0) or 0))
    if not rows:
        return
    all_done = all(str(row.get("阶段状态") or "") == "已完成" for row in rows)
    if all_done:
        entry["status"] = "已完成"
        entry["同步状态"] = "已完成"
        entry["pending"] = False
        entry["最后同步时间"] = now_text()
    else:
        entry["status"] = "同步中"
        entry["同步状态"] = "同步中"
        entry["pending"] = True


def _resolve_conflicts(entry: dict[str, Any], stage_no: int, reason: str) -> None:
    for conflict in conflicts_for_task(int(entry.get("id", 0) or 0)):
        if conflict.get("状态") != "待处理":
            continue
        if conflict.get("阶段序号") not in (None, stage_no):
            continue
        conflict["状态"] = "已解决"
        conflict["pending"] = False
        conflict["解决说明"] = reason
        conflict["解决时间"] = now_text()


def _add_conflict(
    entry: dict[str, Any],
    conflict_type: str,
    stage_no: int | None,
    reason: str,
    node_active: dict[str, Any] | None,
) -> dict[str, Any]:
    rows = store.rows(CONFLICT_MODULE)
    fingerprint = f"{entry.get('任务编号')}|{entry.get('配置版本号')}|{conflict_type}|{stage_no}"
    for row in rows:
        if row.get("冲突指纹") == fingerprint and row.get("状态") == "待处理":
            return row
    conflict = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "冲突指纹": fingerprint,
        "任务ID": entry.get("id"),
        "任务编号": entry.get("任务编号"),
        "目标节点": entry.get("目标节点"),
        "配置版本号": entry.get("配置版本号"),
        "冲突类型": conflict_type,
        "阶段序号": stage_no,
        "原因": reason,
        "节点当前版本": node_active.get("配置版本号") if node_active else None,
        "节点依据": {
            "节点编号": entry.get("目标节点"),
            "节点当前版本": node_active.get("配置版本号") if node_active else None,
        },
        "旧载荷依据": {
            "旧载荷版本": str(entry.get("payload_version") or "legacy-v1"),
            "配置版本号": entry.get("配置版本号"),
            "旧审计次数": len(_audit_rows(int(entry.get("id", 0) or 0))),
        },
        "状态": "待处理",
        "pending": True,
        "abnormal": True,
    }
    rows.append(conflict)
    return conflict


def pending_stage_count(entry: dict[str, Any]) -> int:
    return sum(
        1
        for row in staged_rows_for_task(int(entry.get("id", 0) or 0))
        if str(row.get("阶段状态") or "") != "已完成"
    )


def migrated_task_ids() -> set[int]:
    return {int(row.get("任务ID", 0) or 0) for row in store.rows(STAGED_MODULE)}


def node_readable_versions(node: str) -> list[dict[str, Any]]:
    return [
        {
            "配置版本号": row.get("配置版本号"),
            "来源任务": row.get("来源任务"),
            "生效时间": row.get("生效时间"),
            "完整": bool(row.get("完整")),
        }
        for row in node_version_rows(node)
        if bool(row.get("完整")) and str(row.get("状态") or "") == "生效"
    ]

