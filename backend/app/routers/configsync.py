"""配置同步接口：维护同步任务，覆盖旧链路动作、分阶段迁移、续做与追溯。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import workterminal
from app.services.configsync import ConfigsyncService

router = APIRouter(prefix="/api/configsync", tags=["配置同步"])

service = ConfigsyncService()

LIST_FIELDS = ["任务编号", "源节点", "目标节点", "同步类型", "最后同步时间", "配置版本号", "差异摘要", "同步状态"]
STATUSES = ["草稿", "同步中", "已完成", "已失败"]
STAGE_ACTIONS = ["应用阶段", "确认阶段"]


def _require_staged_enabled() -> None:
    if not settings.staged_sync_enabled:
        raise HTTPException(
            status_code=409,
            detail="分阶段同步入口已关闭（SYNC_STAGED_ENABLED=0）；旧链路动作仍可用",
        )


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="草稿、同步中、已完成、已失败"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤配置同步列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出配置同步清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "configsync", "total": total, "items": items}


@router.get("/results")
def list_result_datasets() -> dict[str, Any]:
    """全部分阶段同步结果数据集：逐项列出阶段、状态、幂等标识与版本依据。"""
    items: list[dict[str, Any]] = []
    for row in service.list_entries(page=1, size=10000)[0]:
        dataset = service.result_dataset(int(row.get("id", 0) or 0))[0]
        if dataset is not None:
            items.append(dataset)
    return {"module": "configsync", "total": len(items), "items": items}


@router.get("/lookup")
def lookup_entries(
    task_no: str | None = Query(default=None, description="按任务编号回溯"),
    source_node: str | None = Query(default=None, description="按源节点回溯"),
    version: str | None = Query(default=None, description="按配置版本号回溯"),
) -> dict[str, Any]:
    """同步任务 / 源节点 / 配置版本号 → 结果、节点版本与审计的追溯入口。"""
    items = service.lookup(task_no=task_no, source_node=source_node, version=version)
    return {"total": len(items), "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条同步任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"同步任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条同步任务；新载荷未给版本号时自动生成明确版本，避免版本号悬空。"""
    entry, missing = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=f"登记失败：{'、'.join(missing)}")
    return ActionResult(ok=True, message="同步任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """旧链路动作：发起同步、确认完成、标记失败。

    旧任务仍按一次性确认推进；新载荷的"确认完成"会被阶段进度拦下，
    必须逐段应用与确认，保证中断后能按已应用段续做。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/migrate", response_model=ActionResult)
def migrate_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """把旧载荷转换成逐段可复核的分阶段同步结果；重复提交按幂等返回既有结果。"""
    _require_staged_enabled()
    result, message = service.migrate(entry_id, payload.values)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/{entry_id}/result", response_model=dict)
def get_result(entry_id: int) -> dict:
    """读取一条任务的分阶段同步结果；未迁移时明确提示，而不是返回空结构。"""
    dataset, message = service.result_dataset(entry_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=message)
    return dataset


@router.post("/{entry_id}/stages/{stage_no}/actions", response_model=ActionResult)
def run_stage_action(entry_id: int, stage_no: int, payload: EntryPayload) -> ActionResult:
    """按阶段应用或确认；同一阶段重复提交按幂等处理，不会重复应用配置。"""
    _require_staged_enabled()
    action = str(payload.values.get("action") or "").strip()
    idempotency_key = str(payload.values.get("幂等标识") or "").strip() or None
    dataset, message = service.stage_action(
        entry_id,
        stage_no,
        action,
        idempotency_key=idempotency_key,
        actor=str(payload.values.get("操作人") or "值班管理员"),
    )
    if dataset is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=dataset)


@router.post("/{entry_id}/replay", response_model=ActionResult)
def replay_entry(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """重放旧任务：只续做未应用段，兼容既有审计，节点版本不倒退。"""
    _require_staged_enabled()
    actor = str((payload.values if payload else {}).get("操作人") or "值班管理员")
    dataset, message = service.replay_legacy(entry_id, actor=actor)
    if dataset is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=dataset)


@router.get("/{entry_id}/trace", response_model=dict)
def trace_entry(entry_id: int) -> dict:
    """追溯：同步任务 → 分阶段同步结果 → 节点配置版本 → 同步审计。"""
    trace, message = service.trace(entry_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=message)
    return trace


@router.get("/{entry_id}/audit", response_model=dict)
def audit_entry(entry_id: int) -> dict:
    """同步审计结果：旧审计原样保留，新阶段动作追加在后。"""
    rows, message = service.audit(entry_id)
    if rows is None:
        raise HTTPException(status_code=404, detail=message)
    return {"任务ID": entry_id, "total": len(rows), "items": rows}


@router.get("/{entry_id}/conflicts", response_model=dict)
def conflict_entries(entry_id: int) -> dict:
    """待处理记录：新旧载荷映射冲突、版本回退等，逐条可解释可复核。"""
    rows = service.conflicts(entry_id)
    return {
        "任务ID": entry_id,
        "total": len(rows),
        "pending": len([row for row in rows if row.get("状态") == "待处理"]),
        "items": rows,
    }


@router.get("/{entry_id}/terminal", response_model=dict)
def terminal_view(entry_id: int) -> dict:
    """工作终端侧读取视图：只暴露一个已完整应用的节点配置版本。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"同步任务 {entry_id} 不存在或已归档")
    node = str(entry.get("目标节点") or "")
    return workterminal.terminal_summary(node)
