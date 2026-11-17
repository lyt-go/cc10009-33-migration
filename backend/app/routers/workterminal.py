"""工作终端侧接口：读取清单、刷新清单、查看清单变更记录。

工作终端是配置下发的消费方，只能看到"已完整应用"的一个节点配置版本；全部
可读版本与冲突线索留在清单的版本说明里供人工复核。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload
from app.services import workterminal
from app.services.configsync_versions import active_node_version, node_version_rows

router = APIRouter(prefix="/api/workterminal", tags=["工作终端"])


@router.get("/manifests")
def list_manifests() -> dict[str, Any]:
    """工作终端清单列表：每个节点一条，摘要可供下游核对。"""
    items = workterminal.list_manifests()
    return {"module": "workterminal", "total": len(items), "items": items}


@router.get("/manifests/{node}", response_model=dict)
def get_manifest(node: str) -> dict:
    """读取单节点清单；还没生成过时给出明确提示。"""
    manifest = workterminal.get_manifest(node)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"节点 {node} 还没有工作终端清单，请先刷新")
    return manifest


@router.post("/manifests/{node}/refresh", response_model=ActionResult)
def refresh_manifest(node: str, payload: EntryPayload | None = None) -> ActionResult:
    """刷新清单并留下一条记录；摘要未变化时记录标记为未变化，不新增清单行。"""
    reason = str((payload.values if payload else {}).get("原因") or "人工刷新工作终端清单")
    manifest = workterminal.refresh_node_manifest(node, reason=reason, trigger="manual")
    return ActionResult(
        ok=True,
        message=f"节点 {node} 清单已刷新，可下发={manifest.get('可下发')}",
        entry=manifest,
    )


@router.get("/manifests/{node}/logs")
def manifest_logs(node: str) -> dict[str, Any]:
    """清单变更记录：每次刷新都留痕，含触发来源、摘要与是否可下发。"""
    items = workterminal.list_logs(node)
    return {"node": node, "total": len(items), "items": items}


@router.get("/nodes/{node}/config", response_model=dict)
def node_config(node: str) -> dict:
    """工作终端读取入口：只返回一个已完整应用的节点配置版本。"""
    return workterminal.terminal_summary(node)


@router.get("/nodes/{node}/versions")
def node_versions(node: str) -> dict[str, Any]:
    """节点配置版本明细：供复核用，包含已替代版本，工作终端本身不读取这里下发。"""
    rows = node_version_rows(node)
    active = active_node_version(node)
    return {
        "node": node,
        "total": len(rows),
        "active": active.get("配置版本号") if active else None,
        "items": rows,
    }


@router.get("/logs")
def all_logs(node: str | None = Query(default=None, description="按节点筛选")) -> dict[str, Any]:
    items = workterminal.list_logs(node)
    return {"total": len(items), "items": items}
