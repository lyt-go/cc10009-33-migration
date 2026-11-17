"""配置同步业务规则：状态流转、字段校验、旧载荷迁移与分阶段应用。

旧接口（发起同步/确认完成/标记失败）保持原样，旧任务仍能按老方式推进；新
载荷按阶段应用，两者共用同一份审计表和节点版本表，保证任务、版本、审计三者
可以互相追溯。
"""
from __future__ import annotations

import threading
from typing import Any

from app.config import settings
from app.services import configsync_legacy as legacy
from app.services import configsync_staged as staged
from app.services.configsync_versions import (
    NODE_VERSION_MODULE,
    active_node_version,
    compare_versions,
    find_node_version,
    next_version_for_node,
    node_version_rows,
    now_text,
    retire_older_node_versions,
)
from app.store import store

MODULE = "configsync"
REQUIRED_FIELDS = ["任务编号", "源节点", "目标节点"]
STATUS_ORDER = ["草稿", "同步中", "已完成", "已失败"]
ACTION_RULES = {"发起同步": "同步中", "确认完成": "已完成", "标记失败": "已失败"}
NEGATIVE_ACTIONS = []

# 迁移与阶段写入共用一把可重入锁：并发提交时只有一个请求能生成阶段结果或写节点版本，
# 其余请求在锁内看到"已迁移/已应用"后按幂等返回。
_MIGRATION_LOCK = threading.RLock()


class ConfigsyncService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        items = [self._decorate(row) for row in rows]
        total = len(items)
        start = max(page - 1, 0) * size
        return items[start:start + size], total

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表里补充分阶段进度，旧字段不动，下游按老字段读也不会断。"""
        item = dict(row)
        item["载荷版本"] = legacy.payload_version_of(row)
        stages = staged.staged_rows_for_task(int(row.get("id", 0) or 0))
        item["分阶段结果已生成"] = bool(stages)
        item["阶段总数"] = len(stages)
        item["已应用阶段数"] = sum(
            1 for stage in stages if str(stage.get("应用状态") or "") == staged.APPLIED
        )
        item["待处理冲突数"] = len(
            [row for row in staged.conflicts_for_task(int(row.get("id", 0) or 0)) if row.get("状态") == "待处理"]
        )
        return item

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._decorate(row) if row else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        task_no = str(values.get("任务编号") or "").strip()
        for row in rows:
            if str(row.get("任务编号") or "").strip() == task_no:
                return None, [f"任务编号 {task_no} 已存在，请改用新的任务编号"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["同步类型"] = values.get("同步类型") or "增量"
        entry["差异摘要"] = values.get("差异摘要") or ""
        entry["最后同步时间"] = values.get("最后同步时间") or now_text()
        entry["payload_version"] = str(values.get("payload_version") or legacy.STAGED_PAYLOAD_VERSION)
        if entry["payload_version"] not in legacy.SUPPORTED_PAYLOAD_VERSIONS:
            return None, [f"载荷版本 {entry['payload_version']} 不受支持"]

        version = str(values.get("配置版本号") or "").strip()
        if version:
            for row in rows:
                if (
                    str(row.get("目标节点") or "") == str(entry["目标节点"])
                    and str(row.get("配置版本号") or "") == version
                ):
                    return None, [
                        f"目标节点 {entry['目标节点']} 已有任务使用版本 {version}，请改用新的版本号"
                    ]
        if not version:
            # 迁移期间新任务必须写入明确版本，避免空版本号在节点比较时被当成最小版本。
            pending = [
                str(row.get("配置版本号") or "")
                for row in store.rows(legacy.STAGED_MODULE)
                if str(row.get("目标节点") or "") == str(entry["目标节点"])
            ]
            version = next_version_for_node(str(entry["目标节点"]), pending)
        entry["配置版本号"] = version
        entry["幂等标识"] = str(values.get("幂等标识") or "").strip() or legacy.idempotency_key(entry)
        if isinstance(values.get("阶段列表"), list):
            entry["阶段列表"] = values.get("阶段列表")

        entry["status"] = STATUS_ORDER[0]
        entry["同步状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)

        if entry["payload_version"] == legacy.STAGED_PAYLOAD_VERSION:
            # 新载荷登记即建立阶段计划，保证阶段标识与幂等键从第一条记录起就固定。
            with _MIGRATION_LOCK:
                legacy.migrate_legacy_payload(entry, actor=str(values.get("操作人") or "值班管理员"))
            entry.update(self._conflict_status(entry))
        return entry, []

    def _conflict_status(self, entry: dict[str, Any]) -> dict[str, Any]:
        pending = [
            row for row in store.rows(legacy.CONFLICT_MODULE)
            if int(row.get("任务ID", 0) or 0) == int(entry.get("id", 0) or 0)
            and row.get("状态") == "待处理"
        ]
        if pending:
            entry["abnormal"] = True
            entry["待处理冲突数"] = len(pending)
        return entry

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同步任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于配置同步可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if action == "确认完成" and not settings.legacy_confirm_enabled:
            return None, "旧链路一次性确认已停用，请按分阶段结果逐段应用与确认"

        if action == "确认完成":
            # 新载荷、以及迁移后还有未应用段的旧任务，都不允许一次性确认：
            # 那会把未应用段直接标成已完成，等于跳过分段续做。
            pending = staged.pending_stage_count(entry)
            if legacy.payload_version_of(entry) != legacy.LEGACY_PAYLOAD_VERSION and pending:
                return None, f"分阶段载荷还有 {pending} 个阶段未完成，请按阶段应用并确认"
            if legacy.payload_version_of(entry) == legacy.LEGACY_PAYLOAD_VERSION and pending:
                return None, (
                    f"旧载荷迁移后还有 {pending} 个阶段未应用，只能续做未应用段，"
                    "不能一次性确认完成"
                )
            blocked = self._legacy_confirm(entry)
            if blocked:
                return None, blocked

        entry["status"] = target
        entry["同步状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self._decorate(entry), f"同步任务已{action}"

    def _legacy_confirm(self, entry: dict[str, Any]) -> str | None:
        """旧载荷"确认完成"保留原语义，但补上节点版本与审计，重放时不重复写。

        返回非空字符串表示被拦下；节点版本只会向前，不会为了完成确认而回退。
        """
        node = str(entry.get("目标节点") or "").strip()
        version = str(entry.get("配置版本号") or "").strip()
        if not version or not node:
            return None
        key = str(entry.get("幂等标识") or legacy.idempotency_key(entry))
        already = any(
            row.get("动作") == "确认完成" and str(row.get("幂等标识") or "") == key
            for row in store.rows(legacy.AUDIT_MODULE)
            if int(row.get("任务ID", 0) or 0) == int(entry.get("id", 0) or 0)
        )
        if already:
            return None
        if find_node_version(node, version) is None:
            active = active_node_version(node)
            if active is not None and compare_versions(version, active.get("配置版本号")) < 0:
                legacy._add_conflict(
                    entry=entry,
                    conflict_type="版本回退",
                    stage_no=None,
                    reason=(
                        f"确认完成会把节点从 {active.get('配置版本号')} 退回 {version}，"
                        "已保留待处理记录，节点版本未改动"
                    ),
                    evidence={"节点生效版本": active.get("配置版本号"), "旧载荷版本": version},
                )
                return (
                    f"目标节点当前生效版本 {active.get('配置版本号')} 高于 {version}，"
                    "已保留待处理记录，未回退节点版本"
                )
            rows = store.rows(NODE_VERSION_MODULE)
            rows.append(
                {
                    "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                    "节点编号": node,
                    "配置版本号": version,
                    "来源任务": entry.get("任务编号"),
                    "来源任务ID": entry.get("id"),
                    "分阶段结果ID": None,
                    "配置内容哈希": f"legacy:{entry.get('任务编号')}:{version}",
                    "完整": True,
                    "状态": "生效",
                    "生效时间": now_text(),
                    "创建时间": now_text(),
                }
            )
            retire_older_node_versions(node, version)
        rows = store.rows(legacy.AUDIT_MODULE)
        rows.append(
            {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "任务ID": entry.get("id"),
                "任务编号": entry.get("任务编号"),
                "动作": "确认完成",
                "原状态": entry.get("status"),
                "新状态": "已完成",
                "载荷版本": legacy.payload_version_of(entry),
                "阶段": "一次性确认",
                "幂等标识": key,
                "操作人": "值班管理员",
                "时间": now_text(),
                "详情": {"配置版本号": version, "节点版本落库": True},
            }
        )
        return None

    # ------------------------------------------------------------------
    # 分阶段迁移
    # ------------------------------------------------------------------
    def migrate(self, entry_id: int, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        """把任务载荷转换成逐段可续做的分阶段结果；重复调用只补缺的阶段。

        迁移只读旧载荷、写阶段计划，不应用任何阶段：中断后的续做由 replay 负责，
        这样迁移本身可以反复调用而不会重复推进配置。
        """
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同步任务 {entry_id} 不存在或已归档"

        with _MIGRATION_LOCK:
            result = legacy.migrate_legacy_payload(
                entry, actor=str(values.get("操作人") or "值班管理员")
            )

        if result.get("待处理冲突"):
            entry["abnormal"] = True
        entry["payload_version"] = str(entry.get("payload_version") or legacy.LEGACY_PAYLOAD_VERSION)
        message = "分阶段同步结果已存在，按幂等返回" if result.get("已存在") else "分阶段同步结果已生成"
        return result, message

    def result_dataset(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同步任务 {entry_id} 不存在或已归档"
        if int(entry.get("id", 0) or 0) not in staged.migrated_task_ids():
            return None, "该任务还没有分阶段同步结果，请先执行迁移"
        return staged.build_result_dataset(entry), "ok"

    def stage_action(
        self,
        entry_id: int,
        stage_no: int,
        action: str,
        *,
        idempotency_key: str | None = None,
        actor: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同步任务 {entry_id} 不存在或已归档"
        if action not in {legacy.STAGE_APPLY_ACTION, legacy.STAGE_CONFIRM_ACTION}:
            return None, f"动作「{action}」不属于分阶段同步可执行范围"
        with _MIGRATION_LOCK:
            if action == legacy.STAGE_APPLY_ACTION:
                stage, message, repeated = staged.apply_stage(
                    entry, stage_no, actor=actor, idempotency_key=idempotency_key
                )
            else:
                stage, message, repeated = staged.confirm_stage(
                    entry, stage_no, actor=actor, idempotency_key=idempotency_key
                )
            if stage is None:
                return None, message
            _refresh_related_business(entry)
            result = staged.build_result_dataset(entry)
            result["本次动作"] = {
                "动作": action,
                "阶段序号": stage_no,
                "重复提交": repeated,
                "说明": message,
            }
            return result, message

    # ------------------------------------------------------------------
    # 旧任务重放与追溯
    # ------------------------------------------------------------------
    def replay_legacy(self, entry_id: int, *, actor: str = "值班管理员") -> tuple[dict[str, Any] | None, str]:
        """重放旧任务：兼容既有审计，只续做未应用段，节点版本只向前不后退。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同步任务 {entry_id} 不存在或已归档"
        if legacy.payload_version_of(entry) != legacy.LEGACY_PAYLOAD_VERSION:
            return None, "只有旧版载荷需要重放；新载荷请直接按阶段续做"
        with _MIGRATION_LOCK:
            if int(entry.get("id", 0) or 0) not in staged.migrated_task_ids():
                legacy.migrate_legacy_payload(entry, actor=actor)
            rows = staged.staged_rows_for_task(int(entry.get("id", 0) or 0))
            if not rows:
                return None, "旧载荷无法转换出任何阶段，已保留原记录待人工核对"
            pending = [row for row in rows if str(row.get("应用状态") or "") != staged.APPLIED]
            if not pending:
                result = staged.build_result_dataset(entry)
                result["重放"] = {"动作": "无", "说明": "旧任务各段均已应用并留有审计，未重复执行"}
                return result, "旧调任务已应用完毕，重放按幂等跳过"

            applied: list[int] = []
            conflicts: list[str] = []
            skipped: list[dict[str, Any]] = []
            for row in pending:
                stage_no = int(row.get("阶段序号", 0) or 0)
                stage, message, _repeated = staged.apply_stage(
                    entry, stage_no, actor=actor, idempotency_key=str(row.get("应用幂等标识") or "") or None
                )
                if stage is None:
                    conflicts.append(message)
                    # 版本回退等冲突只记录一次，剩余段不再硬推，避免留下半套版本。
                    remaining = [
                        int(item.get("阶段序号", 0) or 0) for item in pending
                        if int(item.get("阶段序号", 0) or 0) >= stage_no
                        and int(item.get("阶段序号", 0) or 0) != stage_no
                    ]
                    skipped = remaining
                    break
                applied.append(stage_no)
            if applied:
                _refresh_related_business(entry)
            result = staged.build_result_dataset(entry)
            result["重放"] = {
                "动作": "续做未应用段" if applied else "未续做",
                "已应用阶段": applied,
                "跳过阶段": skipped,
                "阻塞原因": conflicts,
                "旧审计保留": True,
            }
            if conflicts:
                return result, f"重放已续做 {len(applied)} 个阶段，其余因冲突保留待处理：{conflicts[0]}"
            return result, f"重放已续做 {len(applied)} 个未应用阶段，旧审计未改写"

    def trace(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """任务 → 分阶段结果 → 节点配置版本 → 审计 的追溯视图。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同步任务 {entry_id} 不存在或已归档"
        node = str(entry.get("目标节点") or "").strip()
        audits = [
            dict(row)
            for row in store.rows(legacy.AUDIT_MODULE)
            if int(row.get("任务ID", 0) or 0) == int(entry.get("id", 0) or 0)
        ]
        versions = [
            dict(row)
            for row in node_version_rows(node)
            if int(row.get("来源任务ID", 0) or 0) == int(entry.get("id", 0) or 0)
            or str(row.get("来源任务") or "") == str(entry.get("任务编号") or "")
        ]
        dataset = None
        if int(entry.get("id", 0) or 0) in staged.migrated_task_ids():
            dataset = staged.build_result_dataset(entry)
        return {
            "同步任务": self._decorate(entry),
            "分阶段同步结果": dataset,
            "节点配置版本": versions,
            "同步审计结果": audits,
            "节点当前生效版本": (
                active_node_version(node).get("配置版本号") if active_node_version(node) else None
            ),
            "关联审计条数": len(audits),
            "关联版本条数": len(versions),
        }, "ok"

    def lookup(
        self,
        *,
        task_no: str | None = None,
        source_node: str | None = None,
        version: str | None = None,
    ) -> list[dict[str, Any]]:
        """按任务编号、源节点、配置版本号回溯到结果、节点版本与审计。"""
        matches: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            if task_no and str(row.get("任务编号") or "") != task_no:
                continue
            if source_node and str(row.get("源节点") or "") != source_node:
                continue
            if version and str(row.get("配置版本号") or "") != version:
                continue
            trace, _message = self.trace(int(row.get("id", 0) or 0))
            matches.append(
                {
                    "任务ID": row.get("id"),
                    "任务编号": row.get("任务编号"),
                    "源节点": row.get("源节点"),
                    "目标节点": row.get("目标节点"),
                    "配置版本号": row.get("配置版本号"),
                    "同步状态": row.get("status"),
                    "分阶段结果": (
                        {
                            "阶段总数": trace["分阶段同步结果"]["阶段总数"],
                            "已应用阶段数": trace["分阶段同步结果"]["已应用阶段数"],
                            "结果摘要": trace["分阶段同步结果"]["结果摘要"],
                        }
                        if trace and trace.get("分阶段同步结果")
                        else None
                    ),
                    "节点配置版本": (trace or {}).get("节点配置版本", []),
                    "同步审计条数": (trace or {}).get("关联审计条数", 0),
                    "追溯入口": f"/api/configsync/{row.get('id')}/trace",
                }
            )
        return matches

    def audit(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同步任务 {entry_id} 不存在或已归档"
        return [
            dict(row)
            for row in store.rows(legacy.AUDIT_MODULE)
            if int(row.get("任务ID", 0) or 0) == int(entry.get("id", 0) or 0)
        ], "ok"

    def conflicts(self, entry_id: int) -> list[dict[str, Any]]:
        return staged.conflicts_for_task(entry_id)


def _refresh_related_business(entry: dict[str, Any]) -> None:
    """阶段推进后刷新关联业务面：工作终端清单与采集节点标签同步最新完整版本。"""
    from app.services.workterminal import refresh_node_manifest

    node = str(entry.get("目标节点") or "").strip()
    if node:
        refresh_node_manifest(node, reason=f"任务 {entry.get('任务编号')} 阶段推进", trigger="configsync")
        for worker in store.rows("worker"):
            if str(worker.get("节点编号") or "") == node:
                active = active_node_version(node)
                if active:
                    worker["当前配置版本"] = active.get("配置版本号")
