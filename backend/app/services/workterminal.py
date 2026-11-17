"""工作终端侧：读取分阶段同步结果，维护本地清单。

工作终端只认已完整应用的节点配置版本：同一节点无论后端留下几组可读版本依据，
清单里只保留一个 ``已完整应用版本``，其余版本作为复核线索放进 ``版本说明``，
不参与下发。清单摘要写入 ``work_terminal_manifests``，每次刷新留一条
``work_terminal_manifest_logs``，供下游核对分阶段同步结果。
"""
from __future__ import annotations

from typing import Any

from app.services.configsync_versions import (
    active_node_version,
    payload_checksum,
    version_sort_key,
)
from app.store import store

MANIFEST_MODULE = "work_terminal_manifests"
LOG_MODULE = "work_terminal_manifest_logs"
NODE_VERSION_MODULE = "node_config_versions"
STAGED_MODULE = "configsync_staged"
CONFLICT_MODULE = "configsync_conflicts"


def _node_versions(node: str) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows(NODE_VERSION_MODULE)
        if str(row.get("节点编号") or "") == node
    ]


def _staged_tasks(node: str) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows(STAGED_MODULE)
        if str(row.get("目标节点") or "") == node
    ]


def _pending_conflicts(node: str) -> list[dict[str, Any]]:
    """与节点当前完整版本冲突的待处理记录。

    旧审计已确认、节点尚未落库的记录会等阶段应用后自动解决；它描述的是
    "新版本还没写完"，此时节点上唯一的完整版本依然可读可下发。真正阻断下发的
    是会让节点版本倒退或映射不上的待处理记录。
    """
    blocking_types = {"版本回退", "映射冲突"}
    return [
        row
        for row in store.rows(CONFLICT_MODULE)
        if str(row.get("目标节点") or "") == node
        and row.get("状态") == "待处理"
        and str(row.get("冲突类型") or "") in blocking_types
    ]


def _staged_result_summaries(staged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按任务汇总阶段进度：清单的"结果摘要"字段，供下游核对分阶段同步结果。"""
    tasks: dict[int, dict[str, Any]] = {}
    for row in staged:
        task_id = int(row.get("任务ID", 0) or 0)
        item = tasks.setdefault(
            task_id,
            {
                "任务ID": task_id,
                "任务编号": row.get("任务编号"),
                "配置版本号": row.get("配置版本号"),
                "阶段总数": 0,
                "已应用阶段数": 0,
                "已确认阶段数": 0,
            },
        )
        item["阶段总数"] += 1
        if str(row.get("应用状态") or "") == "已应用":
            item["已应用阶段数"] += 1
        if str(row.get("确认状态") or "") == "已确认":
            item["已确认阶段数"] += 1
    return sorted(tasks.values(), key=lambda item: int(item["任务ID"]))


def build_manifest(node: str) -> dict[str, Any]:
    """为一个工作终端生成清单内容；只暴露一个完整应用的版本。"""
    versions = _node_versions(node)
    complete = [
        row
        for row in versions
        if bool(row.get("完整")) and str(row.get("状态") or "") == "生效"
    ]
    active = active_node_version(node)
    ordered = sorted(
        complete,
        key=lambda row: (version_sort_key(row.get("配置版本号")), int(row.get("id", 0))),
    )
    conflicts = _pending_conflicts(node)
    all_conflicts = [
        row
        for row in store.rows(CONFLICT_MODULE)
        if str(row.get("目标节点") or "") == node and row.get("状态") == "待处理"
    ]
    staged = _staged_tasks(node)
    readable = [
        {
            "配置版本号": row.get("配置版本号"),
            "来源任务": row.get("来源任务"),
            "生效时间": row.get("生效时间"),
        }
        for row in ordered
    ]
    staged_tasks = _staged_result_summaries(staged)
    # 已应用但未确认的阶段说明节点正在收尾写版本，此时不推送新清单；
    # 只是"还没开始应用"的阶段不影响当前完整版本继续被读取。
    in_flight = [
        row
        for row in staged
        if str(row.get("应用状态") or "") == "已应用"
        and str(row.get("确认状态") or "") != "已确认"
    ]
    exclusive = active or (ordered[-1] if ordered else None)
    # 节点上有一个以上完整生效版本时，说明版本依据还没收敛：清单保留最高版本供复核，
    # 但标记为不可下发，等旧版本被替代后自动恢复。
    ready = bool(exclusive) and len(ordered) == 1 and not conflicts and not in_flight
    return {
        "节点编号": node,
        "已完整应用版本": (
            {
                "配置版本号": exclusive.get("配置版本号"),
                "来源任务": exclusive.get("来源任务"),
                "来源任务ID": exclusive.get("来源任务ID"),
                "分阶段结果ID": exclusive.get("分阶段结果ID"),
                "生效时间": exclusive.get("生效时间"),
                "配置内容哈希": exclusive.get("配置内容哈希"),
            }
            if exclusive
            else None
        ),
        "当前生效版本": active.get("配置版本号") if active else None,
        "可读版本数": len(ordered),
        "版本说明": readable,
        "待处理冲突数": len(conflicts),
        "待处理冲突": [
            {
                "任务编号": row.get("任务编号"),
                "冲突类型": row.get("冲突类型"),
                "原因": row.get("原因"),
            }
            for row in conflicts
        ],
        # 不阻断下发的待处理记录（例如"已确认未落库"）也一并记录，供人工核对。
        "复核记录": [
            {
                "任务编号": row.get("任务编号"),
                "冲突类型": row.get("冲突类型"),
                "原因": row.get("原因"),
            }
            for row in all_conflicts
            if row not in conflicts
        ],
        "分阶段任务数": len({int(row.get("任务ID", 0) or 0) for row in staged}),
        "结果摘要": staged_tasks,
        # 清单不隐藏仍在推进的阶段；"可下发"只按已完整应用的版本判定。
        "未完成阶段数": len(in_flight),
        "可下发": ready,
        "清单摘要": payload_checksum(
            {
                "节点编号": node,
                "已完整应用版本": exclusive.get("配置版本号") if exclusive else None,
                "来源任务": exclusive.get("来源任务") if exclusive else None,
                "可读版本": [item["配置版本号"] for item in readable],
                "待处理冲突": len(conflicts),
                "结果摘要": [
                    (item["任务编号"], item["已应用阶段数"], item["阶段总数"])
                    for item in staged_tasks
                ],
                "未完成阶段数": len(in_flight),
            }
        ),
        "生成时间": None,
    }


def _find_manifest(node: str) -> dict[str, Any] | None:
    for row in store.rows(MANIFEST_MODULE):
        if str(row.get("节点编号") or "") == node:
            return row
    return None


def refresh_node_manifest(node: str, *, reason: str, trigger: str = "manual") -> dict[str, Any]:
    """刷新清单并留记录；同一摘要重复刷新只记一条"未变化"，不新增清单行。"""
    from app.services.configsync_versions import now_text

    content = build_manifest(node)
    stamp = now_text()
    content["生成时间"] = stamp
    existing = _find_manifest(node)
    logs = store.rows(LOG_MODULE)
    if existing is None:
        manifest = {"id": max((int(row.get("id", 0)) for row in store.rows(MANIFEST_MODULE)), default=0) + 1}
        manifest.update(content)
        manifest["更新时间"] = stamp
        manifest["pending"] = not content["可下发"]
        manifest["abnormal"] = content["待处理冲突数"] > 0
        store.rows(MANIFEST_MODULE).append(manifest)
        changed = True
    else:
        changed = str(existing.get("清单摘要") or "") != content["清单摘要"]
        existing.update(content)
        existing["更新时间"] = stamp
        existing["pending"] = not content["可下发"]
        existing["abnormal"] = content["待处理冲突数"] > 0
        manifest = existing

    logs.append(
        {
            "id": max((int(row.get("id", 0)) for row in logs), default=0) + 1,
            "清单ID": manifest.get("id"),
            "节点编号": node,
            "触发来源": trigger,
            "原因": reason,
            "变化": changed,
            "清单摘要": content["清单摘要"],
            "已完整应用版本": (
                content["已完整应用版本"]["配置版本号"]
                if content["已完整应用版本"]
                else None
            ),
            "可下发": content["可下发"],
            "时间": stamp,
        }
    )
    return manifest


def list_manifests() -> list[dict[str, Any]]:
    return store.rows(MANIFEST_MODULE)


def list_logs(node: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows(LOG_MODULE)
    if node:
        return [row for row in rows if str(row.get("节点编号") or "") == node]
    return rows


def get_manifest(node: str) -> dict[str, Any] | None:
    return _find_manifest(node)


def terminal_summary(node: str) -> dict[str, Any]:
    """工作终端侧读取入口：只返回一个已完整应用的节点配置版本。"""
    manifest = _find_manifest(node)
    content = build_manifest(node)
    if manifest is None:
        manifest = refresh_node_manifest(node, reason="工作终端首次读取，生成清单", trigger="terminal-read")
    return {
        "节点编号": node,
        "已完整应用版本": content["已完整应用版本"],
        "当前生效版本": content["当前生效版本"],
        "可下发": content["可下发"],
        "待处理冲突数": content["待处理冲突数"],
        "清单摘要": content["清单摘要"],
        "清单更新时间": manifest.get("更新时间"),
        "说明": (
            "清单只保留一个已完整应用的版本"
            if content["可下发"]
            else "存在多组版本依据或待处理冲突，工作终端不下发"
        ),
    }
