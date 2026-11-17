"""配置版本号比较、内容校验和与节点生效版本查询。

配置同步的分阶段迁移需要判断"目标版本是否落后于节点已生效版本"，这类判断
必须收在一处：版本号可能写成 ``v3``、``3`` 或历史遗留的非数字标识，比较口径
不一致就会让迁移把节点版本改回去。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from app.store import store

NODE_VERSION_MODULE = "node_config_versions"
_VERSION_PATTERN = re.compile(r"^\s*[vV]?\s*(\d+)")


def now_text() -> str:
    """统一的时间文本，精确到秒，便于审计与清单排序。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_version(value: Any) -> int | None:
    """把 ``v3`` / ``3`` / ``V03`` 解析成整数；非数字版本返回 None。"""
    match = _VERSION_PATTERN.match(str(value or ""))
    if match is None:
        return None
    return int(match.group(1))


def compare_versions(left: Any, right: Any) -> int:
    """返回 -1/0/1。两边都能解析成数字时按数字比较，否则按文本比较。"""
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    left_number = parse_version(left_text)
    right_number = parse_version(right_text)
    if left_number is not None and right_number is not None:
        return (left_number > right_number) - (left_number < right_number)
    if left_number is not None:
        return 1
    if right_number is not None:
        return -1
    return (left_text > right_text) - (left_text < right_text)


def version_sort_key(value: Any) -> tuple[int, int, str]:
    """排序用键：数字版本优先，数字大的靠后；文本版本按字典序排在后面。"""
    text = str(value or "").strip()
    number = parse_version(text)
    if number is None:
        return (0, -1, text)
    return (1, number, text)


def node_version_rows(node: str) -> list[dict[str, Any]]:
    return [
        row
        for row in store.rows(NODE_VERSION_MODULE)
        if str(row.get("节点编号") or "") == node
    ]


def complete_node_versions(node: str) -> list[dict[str, Any]]:
    """节点上"完整且生效"的版本记录。只有这类记录允许暴露给工作终端。"""
    return [
        row
        for row in node_version_rows(node)
        if bool(row.get("完整")) and str(row.get("状态") or "") == "生效"
    ]


def active_node_version(node: str) -> dict[str, Any] | None:
    """节点当前生效版本；版本号无法排序时用 id 兜底，保证结果确定。"""
    rows = complete_node_versions(node)
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (version_sort_key(row.get("配置版本号")), int(row.get("id", 0))),
    )


def find_node_version(node: str, version: Any) -> dict[str, Any] | None:
    target = str(version or "").strip()
    for row in complete_node_versions(node):
        if str(row.get("配置版本号") or "").strip() == target:
            return row
    return None


def node_version_record(node: str, version: Any) -> dict[str, Any] | None:
    """不区分状态地找节点上某个版本号的第一条记录，用于补齐/暂存版本。"""
    target = str(version or "").strip()
    for row in node_version_rows(node):
        if str(row.get("配置版本号") or "").strip() == target:
            return row
    return None


def retire_older_node_versions(node: str, version: Any) -> list[str]:
    """新版本完整生效后，把更低的完整版本标成"已替代"。

    记录不删除、字段不改写，只把状态从"生效"改成"已替代"，所以旧版本仍可追溯，
    但节点只对工作终端暴露一个完整应用版本。
    """
    retired: list[str] = []
    for row in node_version_rows(node):
        if str(row.get("状态") or "") != "生效":
            continue
        if compare_versions(row.get("配置版本号"), version) < 0:
            row["状态"] = "已替代"
            row["替代时间"] = now_text()
            row["替代版本"] = str(version)
            retired.append(str(row.get("配置版本号")))
    return retired


def next_version_for_node(node: str, pending_versions: list[str] | None = None) -> str:
    """给迁移期间新登记的任务生成明确版本号，避开节点与待应用阶段已占用的号。

    除了节点版本表和待应用阶段，还要避开同一目标节点上其他同步任务已登记的
    版本号：同一节点两个任务共用一个版本号，会让工作终端清单无法判断来源。
    """
    candidates: list[str] = []
    for row in node_version_rows(node):
        candidates.append(str(row.get("配置版本号") or ""))
    for row in store.rows("configsync"):
        if str(row.get("目标节点") or "") == node:
            candidates.append(str(row.get("配置版本号") or ""))
    candidates.extend(pending_versions or [])
    numbers = [number for number in (parse_version(item) for item in candidates) if number is not None]
    return f"v{max(numbers, default=0) + 1}"


def payload_checksum(payload: Any) -> str:
    """稳定序列化后取摘要；同一份载荷无论进程重启都得到同一校验和。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
