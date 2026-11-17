"""配置同步旧载荷的读取与分段转换。

旧版载荷只有"发起同步 / 确认完成 / 标记失败"三个一次性动作，确认即整体完成，
中断后没有"哪些段已经应用"的记录。迁移时把旧载荷转换成按段（阶段）应用的计划：

* 旧载荷里的差异摘要（``差异摘要``）按逗号/顿号/换行拆成若干段；
* 有 ``阶段列表`` 时优先使用显式阶段，每段的顺序与幂等标识由载荷给出；
* 旧审计、节点生效版本作为"已应用依据"合并进来，转换结果放进
  ``configsync_staged``，逐项记录已应用/未应用/冲突。

转换只读输入，不修改旧记录；旧载荷和旧审计原样保留，可追溯。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.configsync_versions import (
    active_node_version,
    compare_versions,
    complete_node_versions,
    find_node_version,
    payload_checksum,
)
from app.store import store

LEGACY_PAYLOAD_VERSION = "legacy-v1"
STAGED_PAYLOAD_VERSION = "staged-v2"
STAGED_MODULE = "configsync_staged"
CONFLICT_MODULE = "configsync_conflicts"
AUDIT_MODULE = "configsync_audit"
MIGRATION_MODULE = "configsync_migrations"

SUPPORTED_PAYLOAD_VERSIONS = {LEGACY_PAYLOAD_VERSION, STAGED_PAYLOAD_VERSION}

# 旧链路里"确认完成"是一次性完成的动作；迁移后每个阶段各自有一对发起/确认动作。
STAGE_APPLY_ACTION = "应用阶段"
STAGE_CONFIRM_ACTION = "确认阶段"

_SPLIT_PATTERN = re.compile(r"[,，、;；\n]+")


def payload_version_of(entry: dict[str, Any]) -> str:
    """读取载荷版本；早期记录没有该字段，按旧版载荷处理。"""
    return str(entry.get("payload_version") or LEGACY_PAYLOAD_VERSION).strip()


def idempotency_key(entry: dict[str, Any], stage_index: int | None = None, action: str = "") -> str:
    """幂等标识：任务 + 版本 + 阶段 + 动作；旧载荷的显式标识原样沿用。

    同一阶段重复提交必须落到同一个键上，服务层据此判断"已经应用过"，而不是
    再写一次节点版本或审计。
    """
    base = str(entry.get("幂等标识") or "").strip()
    if not base:
        base = f"sync:{entry.get('任务编号') or entry.get('id')}:{entry.get('配置版本号') or 'v1'}"
    if stage_index is None:
        return base
    return f"{base}:stage-{stage_index:02d}:{action}"


def _legacy_stage_names(entry: dict[str, Any]) -> list[str]:
    explicit = entry.get("阶段列表")
    if isinstance(explicit, list) and explicit:
        names: list[str] = []
        for item in explicit:
            if isinstance(item, dict):
                name = str(item.get("阶段名") or item.get("name") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                names.append(name)
        if names:
            return names
    summary = str(entry.get("差异摘要") or "").strip()
    names = [part.strip() for part in _SPLIT_PATTERN.split(summary) if part.strip()]
    return names or ["默认配置段"]


def _audit_rows(task_id: int) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows(AUDIT_MODULE)
        if int(row.get("任务ID", 0) or 0) == task_id
    ]


def legacy_applied_evidence(entry: dict[str, Any]) -> dict[str, Any]:
    """从旧审计和节点版本里找"这段/这个版本已经应用过"的依据。

    旧审计写"确认完成"并不代表节点上真的落了版本（CONF-0004 就是这种），所以
    两处依据分开返回，由调用方决定是幂等命中还是版本冲突。
    """
    task_id = int(entry.get("id", 0) or 0)
    version = str(entry.get("配置版本号") or "").strip()
    audits = _audit_rows(task_id)
    confirmed_audits = [row for row in audits if row.get("动作") == "确认完成"]
    node_row = find_node_version(str(entry.get("目标节点") or ""), version)
    return {
        "审计记录": len(audits),
        "确认完成审计": len(confirmed_audits),
        "确认时间": confirmed_audits[-1]["时间"] if confirmed_audits else None,
        "节点已落库": node_row is not None,
        "节点版本记录": node_row,
    }


def target_node_snapshot(node: str) -> dict[str, Any]:
    """目标节点版本快照：完整生效版本、未完整版本都列出来，供迁移逐项复核。"""
    versions = complete_node_versions(node)
    active = active_node_version(node)
    readable = [
        {
            "配置版本号": row.get("配置版本号"),
            "来源任务": row.get("来源任务"),
            "状态": row.get("状态"),
            "完整": bool(row.get("完整")),
        }
        for row in versions
    ]
    return {
        "节点编号": node,
        "生效版本": active.get("配置版本号") if active else None,
        "可读版本": readable,
        "版本数": len(readable),
        # 同节点出现两个及以上完整生效版本，说明旧确认和新阶段同时留了依据。
        "版本依据多组": len(readable) > 1,
    }


def legacy_applied_segments(entry: dict[str, Any]) -> list[str]:
    """旧载荷里已经应用过的段名。

    旧链路没有逐段表，只能从旧载荷携带的分段记录（``已应用段``）里读；这是
    迁移中断后"只能续做未应用段"的唯一依据，读不到就整段重做会被当成重复应用。
    """
    raw = entry.get("已应用段")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("阶段名") or item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            names.append(name)
    return names


def build_stage_plan(entry: dict[str, Any]) -> dict[str, Any]:
    """读取旧载荷与目标节点数据，生成分阶段同步计划（不落库）。"""
    node = str(entry.get("目标节点") or "").strip()
    version = str(entry.get("配置版本号") or "").strip()
    names = _legacy_stage_names(entry)
    applied_segments = set(legacy_applied_segments(entry))
    evidence = legacy_applied_evidence(entry)
    snapshot = target_node_snapshot(node)
    base_key = idempotency_key(entry)

    stages: list[dict[str, Any]] = []
    for index, name in enumerate(names, start=1):
        stages.append(
            {
                "阶段序号": index,
                "阶段名": name,
                "幂等标识": f"{base_key}:stage-{index:02d}",
                "应用幂等标识": idempotency_key(entry, index, STAGE_APPLY_ACTION),
                "确认幂等标识": idempotency_key(entry, index, STAGE_CONFIRM_ACTION),
                "旧载荷已应用": name in applied_segments,
            }
        )

    latest_audit = max(_audit_rows(int(entry.get("id", 0) or 0)), key=lambda row: int(row.get("id", 0)), default=None)
    already_complete = (
        str(entry.get("status") or "") == "已完成"
        and evidence["确认完成审计"] > 0
        and evidence["节点已落库"]
    )
    node_active = active_node_version(node)
    node_ahead = bool(node_active) and compare_versions(version, node_active.get("配置版本号")) < 0

    return {
        "任务ID": entry.get("id"),
        "任务编号": entry.get("任务编号"),
        "源节点": entry.get("源节点"),
        "目标节点": node,
        "配置版本号": version,
        "同步类型": entry.get("同步类型"),
        "旧载荷版本": payload_version_of(entry),
        "旧载荷摘要": payload_checksum(
            {
                "任务编号": entry.get("任务编号"),
                "源节点": entry.get("源节点"),
                "目标节点": node,
                "配置版本号": version,
                "差异摘要": entry.get("差异摘要"),
                "阶段": names,
            }
        ),
        "幂等标识": base_key,
        "阶段": stages,
        "旧审计": {
            "审计记录数": evidence["审计记录"],
            "确认完成次数": evidence["确认完成审计"],
            "最后动作": latest_audit.get("动作") if latest_audit else None,
            "最后时间": latest_audit.get("时间") if latest_audit else None,
        },
        "节点快照": snapshot,
        "节点已落库": evidence["节点已落库"],
        "已确认未落库": evidence["确认完成审计"] > 0 and not evidence["节点已落库"],
        "已整体应用": already_complete,
        "节点版本领先": node_ahead,
        "节点当前版本": node_active.get("配置版本号") if node_active else None,
        "待处理原因": (
            "目标节点已存在同一版本的生效记录，按已应用段处理"
            if already_complete
            else ("目标节点版本高于本任务版本，保留待处理记录，不覆盖节点" if node_ahead else None)
        ),
    }


def _existing_staged_task(task_id: int) -> dict[str, Any] | None:
    for row in store.rows(STAGED_MODULE):
        if int(row.get("任务ID", 0) or 0) == task_id:
            return row
    return None


def detect_mapping_conflict(entry: dict[str, Any]) -> dict[str, Any] | None:
    """新旧载荷映射冲突：同一目标节点上版本号和幂等标识只对上一半。

    同一个节点、同一个版本号却挂着两个不同的幂等标识（或反过来），说明两条
    任务对"这次同步是哪一次"的理解不一致；这种记录不丢弃，也不猜哪边对，
    由调用方记成可解释的待处理记录。
    """
    node = str(entry.get("目标节点") or "").strip()
    version = str(entry.get("配置版本号") or "").strip()
    key = str(entry.get("幂等标识") or "").strip()
    if not node or not version or not key:
        return None
    for row in store.rows("configsync"):
        if int(row.get("id", 0) or 0) == int(entry.get("id", 0) or 0):
            continue
        if str(row.get("目标节点") or "").strip() != node:
            continue
        other_version = str(row.get("配置版本号") or "").strip()
        other_key = str(row.get("幂等标识") or "").strip()
        if not other_key:
            continue
        same_version = other_version == version
        same_key = other_key == key
        if same_version != same_key:
            return row
    return None


def staged_rows_for_task(task_id: int) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows(STAGED_MODULE)
        if int(row.get("任务ID", 0) or 0) == task_id
    ]


def conflicts_for_task(task_id: int) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows(CONFLICT_MODULE)
        if int(row.get("任务ID", 0) or 0) == task_id
    ]


def _add_conflict(
    *,
    entry: dict[str, Any],
    conflict_type: str,
    stage_no: int | None,
    reason: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    rows = store.rows(CONFLICT_MODULE)
    fingerprint = (
        f"{entry.get('任务编号')}|{entry.get('配置版本号')}|{conflict_type}|{stage_no}"
    )
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
        "节点依据": target_node_snapshot(str(entry.get("目标节点") or "")),
        "旧载荷依据": evidence,
        "状态": "待处理",
        "pending": True,
        "abnormal": True,
    }
    rows.append(conflict)
    return conflict


def _new_stage_row(
    *,
    entry: dict[str, Any],
    stage: dict[str, Any],
    applied: bool,
    source: str,
    applied_at: str | None,
) -> dict[str, Any]:
    rows = store.rows(STAGED_MODULE)
    return {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "任务ID": entry.get("id"),
        "任务编号": entry.get("任务编号"),
        "目标节点": entry.get("目标节点"),
        "配置版本号": entry.get("配置版本号"),
        "阶段序号": stage["阶段序号"],
        "阶段名": stage["阶段名"],
        "幂等标识": stage["应用幂等标识"],
        "应用幂等标识": stage["应用幂等标识"],
        "确认幂等标识": stage["确认幂等标识"],
        "应用状态": "已应用" if applied else "未应用",
        "确认状态": "已确认" if applied else "未确认",
        "阶段状态": "已完成" if applied else "待执行",
        "来源": source,
        "应用时间": applied_at,
        "载荷摘要": payload_checksum(stage),
        "pending": not applied,
        "abnormal": False,
    }


def migrate_legacy_payload(entry: dict[str, Any], *, actor: str = "值班管理员") -> dict[str, Any]:
    """把一条旧载荷转换成逐段可复核的分阶段同步结果（幂等）。

    已经迁移过的任务只返回既有结果，不会重复插入阶段行或冲突行；旧审计
    和节点版本记录一律不改。
    """
    task_id = int(entry.get("id", 0) or 0)
    existing = _existing_staged_task(task_id)
    plan = build_stage_plan(entry)

    if existing is not None:
        from app.services.configsync_staged import build_result_dataset

        return {
            "已存在": True,
            "计划": plan,
            "结果": build_result_dataset(entry),
        }

    now = plan["旧审计"].get("最后时间") or ""
    stage_rows: list[dict[str, Any]] = []
    # 旧链路"确认完成"是整份载荷一起确认的；节点上已经有该版本才算真正应用。
    # 确认了但节点没落库（中断）时，只把节点上现存的版本标成已应用，其余留给续做。
    readable_versions = [item for item in plan["节点快照"]["可读版本"] if item.get("完整")]
    latest_node_version = readable_versions[-1] if readable_versions else None
    for stage in plan["阶段"]:
        # 已应用段来自旧载荷里的分段记录；整体确认 + 节点已落库则全段视为已应用。
        applied = bool(plan["已整体应用"] or stage.get("旧载荷已应用"))
        source = "旧审计" if plan["已整体应用"] else ("旧载荷分段记录" if applied else "待迁移")
        applied_at = now if applied else None
        row = _new_stage_row(
            entry=entry,
            stage=stage,
            applied=applied,
            source=source,
            applied_at=applied_at,
        )
        if plan["节点版本领先"]:
            # 版本冲突时整条任务保留待处理：不判成已应用，也不允许应用/确认推进。
            row["阶段状态"] = "待处理冲突"
            row["abnormal"] = True
            row["来源"] = "版本冲突"
        if plan["已确认未落库"]:
            # 旧确认完成的版本没落库：记录两份依据，但阶段仍算未应用，允许续做补齐。
            row["旧确认未落库"] = True
            row["已确认版本"] = plan["配置版本号"]
            if latest_node_version is not None:
                row["节点现存版本"] = latest_node_version.get("配置版本号")
        store.rows(STAGED_MODULE).append(row)
        stage_rows.append(row)

    mapping_conflict = detect_mapping_conflict(entry)
    if mapping_conflict is not None:
        # 同节点同版本挂着不同幂等标识（或反过来）：两条任务对"这次同步"的理解不一致，
        # 不能二选一，保留待处理记录，同时把阶段挂起，避免在歧义上继续推进。
        for row in stage_rows:
            row["阶段状态"] = "待处理冲突"
            row["abnormal"] = True
        _add_conflict(
            entry=entry,
            conflict_type="映射冲突",
            stage_no=None,
            reason=(
                f"任务 {entry.get('任务编号')} 与 {mapping_conflict.get('任务编号')} "
                f"在目标节点 {plan['目标节点']} 上的版本号与幂等标识映射不一致；"
                "保留待处理记录，不覆盖任何一方"
            ),
            evidence={
                "本任务": {
                    "配置版本号": plan["配置版本号"],
                    "幂等标识": plan["幂等标识"],
                },
                "冲突任务": {
                    "任务编号": mapping_conflict.get("任务编号"),
                    "配置版本号": mapping_conflict.get("配置版本号"),
                    "幂等标识": mapping_conflict.get("幂等标识"),
                },
            },
        )

    if plan["已确认未落库"]:
        # 旧审计写着"确认完成 vN"，节点上却只有别的版本：两路依据都成立，记为待处理，
        # 等阶段真正应用、节点版本补齐后由应用动作自动解决。
        _add_conflict(
            entry=entry,
            conflict_type="版本依据",
            stage_no=None,
            reason=(
                f"旧载荷已确认完成版本 {plan['配置版本号']}，但目标节点尚未落库该版本；"
                "旧审计与节点版本记录同时给出可读依据"
            ),
            evidence={
                "旧载荷版本": plan["配置版本号"],
                "旧审计": plan["旧审计"],
                "节点可读版本": plan["节点快照"]["可读版本"],
            },
        )

    if plan["节点版本领先"]:
        _add_conflict(
            entry=entry,
            conflict_type="版本回退",
            stage_no=None,
            reason=(
                f"目标节点当前生效版本 {plan['节点当前版本']} 高于旧载荷版本 "
                f"{plan['配置版本号']}，迁移只记录待处理，不回写节点版本"
            ),
            evidence={
                "旧载荷版本": plan["配置版本号"],
                "节点生效版本": plan["节点当前版本"],
                "旧审计": plan["旧审计"],
            },
        )

    from app.services.configsync_staged import build_result_dataset, version_evidence

    document = {
        "任务ID": plan["任务ID"],
        "任务编号": plan["任务编号"],
        "目标节点": plan["目标节点"],
        "配置版本号": plan["配置版本号"],
        "旧载荷版本": plan["旧载荷版本"],
        "旧载荷摘要": plan["旧载荷摘要"],
        "阶段数": len(stage_rows),
        "节点快照": plan["节点快照"],
        "版本依据": version_evidence(entry),
        "两组版本依据": plan["已确认未落库"],
        "说明": plan["待处理原因"] or "旧载荷已转换为分阶段记录，未应用段等待续做",
    }
    store.rows(MIGRATION_MODULE).append(
        {
            "id": max((int(row.get("id", 0)) for row in store.rows(MIGRATION_MODULE)), default=0) + 1,
            "任务ID": plan["任务ID"],
            "任务编号": plan["任务编号"],
            "目标节点": plan["目标节点"],
            "配置版本号": plan["配置版本号"],
            "旧载荷版本": plan["旧载荷版本"],
            "旧载荷摘要": plan["旧载荷摘要"],
            "阶段数": len(stage_rows),
            "已应用段数": sum(1 for row in stage_rows if row.get("应用状态") == "已应用"),
            "节点快照": plan["节点快照"],
            "两组版本依据": plan["已确认未落库"],
            "操作人": actor,
            "时间": now or "",
            "文档": document,
        }
    )

    return {
        "已存在": False,
        "计划": plan,
        "结果": build_result_dataset(entry),
        "待处理冲突": conflicts_for_task(task_id),
    }
