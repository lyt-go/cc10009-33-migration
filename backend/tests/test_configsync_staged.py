"""配置同步分阶段迁移的行为测试：旧载荷兼容、幂等、并发、版本不倒退。"""
from __future__ import annotations

import threading
import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.store import store


class ConfigsyncStagedCase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例从示例数据重新开始，避免迁移写入在用例之间互相污染。
        store.reset()
        self.client = TestClient(app)

    def migrate(self, entry_id: int):
        response = self.client.post(f"/api/configsync/{entry_id}/migrate", json={"values": {}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def result(self, entry_id: int) -> dict:
        return self.client.get(f"/api/configsync/{entry_id}/result").json()

    def apply(self, entry_id: int, stage_no: int, key: str | None = None):
        values: dict[str, object] = {"action": "应用阶段"}
        if key is not None:
            values["幂等标识"] = key
        return self.client.post(f"/api/configsync/{entry_id}/stages/{stage_no}/actions", json={"values": values})

    def confirm(self, entry_id: int, stage_no: int, key: str | None = None):
        values: dict[str, object] = {"action": "确认阶段"}
        if key is not None:
            values["幂等标识"] = key
        return self.client.post(f"/api/configsync/{entry_id}/stages/{stage_no}/actions", json={"values": values})

    # -- 旧载荷与分段转换 -------------------------------------------------
    def test_legacy_payload_is_split_into_reviewable_stages(self) -> None:
        payload = self.migrate(4)
        dataset = payload["entry"]["结果"]
        self.assertEqual(dataset["载荷版本"], "legacy-v1")
        self.assertEqual([stage["阶段名"] for stage in dataset["阶段"]], ["抓取规则", "限速策略", "重试队列"])
        for stage in dataset["阶段"]:
            self.assertIn(f"stage-{stage['阶段序号']:02d}:应用阶段", stage["应用幂等标识"])
            self.assertEqual(stage["载荷摘要"], stage["载荷摘要"])

    def test_migrate_is_idempotent(self) -> None:
        first = self.migrate(4)
        staged_before = len(store.rows("configsync_staged"))
        second = self.migrate(4)
        self.assertTrue(second["entry"]["已存在"])
        self.assertEqual(len(store.rows("configsync_staged")), staged_before)
        self.assertEqual(
            first["entry"]["结果"]["数据集摘要"], second["entry"]["结果"]["数据集摘要"]
        )

    def test_confirmed_but_unwritten_version_exposes_two_bases(self) -> None:
        dataset = self.migrate(4)["entry"]["结果"]
        self.assertTrue(dataset["两组版本依据"])
        kinds = {item["依据类型"] for item in dataset["版本依据"]}
        self.assertEqual(kinds, {"旧审计", "节点版本记录"})
        self.assertGreaterEqual(dataset["待处理冲突数"], 1)
        terminal = self.client.get("/api/workterminal/nodes/WORK-0002/config").json()
        self.assertEqual(terminal["已完整应用版本"]["配置版本号"], "v2")
        self.assertTrue(terminal["可下发"])

    # -- 阶段应用与幂等 ---------------------------------------------------
    def test_stage_must_apply_in_order(self) -> None:
        self.migrate(4)
        response = self.apply(4, 2)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertIn("只能按顺序续做", response.json()["message"])

    def test_repeated_stage_submission_does_not_reapply(self) -> None:
        self.migrate(4)
        first = self.apply(4, 1).json()
        self.assertTrue(first["ok"])
        versions_after_first = len(store.rows("node_config_versions"))
        audits_after_first = len([row for row in store.rows("configsync_audit") if row["任务ID"] == 4])

        second = self.apply(4, 1).json()
        self.assertTrue(second["ok"])
        self.assertTrue(second["entry"]["本次动作"]["重复提交"])
        self.assertEqual(len(store.rows("node_config_versions")), versions_after_first)
        self.assertEqual(
            len([row for row in store.rows("configsync_audit") if row["任务ID"] == 4]),
            audits_after_first,
        )
        self.assertEqual(len(store.rows("configsync_staged")), 3)

    def test_stage_idempotency_key_must_match(self) -> None:
        self.migrate(4)
        response = self.apply(4, 1, key="sync:CONF-0004:v3:stage-09:应用阶段")
        self.assertFalse(response.json()["ok"])
        self.assertIn("幂等标识不匹配", response.json()["message"])

    def test_confirm_requires_applied_stage(self) -> None:
        self.migrate(4)
        response = self.confirm(4, 1)
        self.assertFalse(response.json()["ok"])
        self.assertIn("尚未应用", response.json()["message"])

    def test_pending_conflicts_block_legacy_one_shot_confirm(self) -> None:
        self.migrate(4)
        response = self.client.post("/api/configsync/4/actions", json={"values": {"action": "确认完成"}})
        self.assertFalse(response.json()["ok"])
        self.assertIn("只能续做未应用段", response.json()["message"])

    def test_full_lifecycle_publishes_single_version(self) -> None:
        self.migrate(4)
        for stage_no in (1, 2, 3):
            self.assertTrue(self.apply(4, stage_no).json()["ok"])
            # 未全部确认前，节点版本不完整、工作终端不下发。
            self.assertFalse(self.client.get("/api/workterminal/nodes/WORK-0002/config").json()["可下发"])
            self.assertTrue(self.confirm(4, stage_no).json()["ok"])
        dataset = self.result(4)
        self.assertEqual(dataset["结果摘要"], "全部阶段已应用并确认")
        self.assertFalse(dataset["两组版本依据"])
        terminal = self.client.get("/api/workterminal/nodes/WORK-0002/config").json()
        self.assertTrue(terminal["可下发"])
        self.assertEqual(terminal["已完整应用版本"]["配置版本号"], "v3")
        task = self.client.get("/api/configsync/4").json()
        self.assertEqual(task["status"], "已完成")

    # -- 中断续做与旧任务重放 ---------------------------------------------
    def test_replay_only_continues_unapplied_stages(self) -> None:
        self.migrate(5)
        before = self.result(5)
        self.assertEqual(before["已应用阶段数"], 1)
        response = self.client.post("/api/configsync/5/replay", json={"values": {}}).json()
        self.assertTrue(response["ok"])
        self.assertEqual(response["entry"]["重放"]["已应用阶段"], [2])
        after = self.result(5)
        self.assertEqual(after["已应用阶段数"], 2)
        # 重放不重写旧审计，只追加阶段审计。
        legacy_audits = [
            row for row in store.rows("configsync_audit")
            if row["任务ID"] == 5 and row["阶段"] == "一次性确认"
        ]
        self.assertEqual(legacy_audits, [])

    def test_replay_does_not_duplicate_completed_task(self) -> None:
        self.migrate(7)
        audits_before = len([row for row in store.rows("configsync_audit") if row["任务ID"] == 7])
        response = self.client.post("/api/configsync/7/replay", json={"values": {}}).json()
        self.assertTrue(response["ok"])
        self.assertEqual(response["entry"]["重放"]["动作"], "无")
        self.assertEqual(
            len([row for row in store.rows("configsync_audit") if row["任务ID"] == 7]), audits_before
        )

    def test_replay_refuses_to_roll_back_node_version(self) -> None:
        # 节点上已有更高的 v5 时，迁移旧载荷 v3 只能留待处理记录，节点版本不变。
        self.migrate(4)
        for row in store.rows("node_config_versions"):
            if row["节点编号"] == "WORK-0002":
                row["状态"] = "已替代"
        store.rows("node_config_versions").append(
            {
                "id": 99,
                "节点编号": "WORK-0002",
                "配置版本号": "v5",
                "来源任务": "CONF-9999",
                "来源任务ID": 9999,
                "分阶段结果ID": None,
                "配置内容哈希": "manual",
                "完整": True,
                "状态": "生效",
                "生效时间": "2026-10-01 10:00",
                "创建时间": "2026-10-01 10:00",
            }
        )
        response = self.client.post("/api/configsync/4/replay", json={"values": {}}).json()
        self.assertTrue(response["ok"])
        self.assertEqual(response["entry"]["重放"]["已应用阶段"], [])
        self.assertIn("拒绝回退", response["entry"]["重放"]["阻塞原因"][0])
        self.assertEqual(response["entry"]["重放"]["跳过阶段"], [2, 3])
        active = self.client.get("/api/workterminal/nodes/WORK-0002/versions").json()["active"]
        self.assertEqual(active, "v5")

    # -- 并发 -------------------------------------------------------------
    def test_concurrent_migration_creates_one_result(self) -> None:
        barrier = threading.Barrier(6)
        outcomes: list[int] = []

        def worker() -> None:
            client = TestClient(app)
            barrier.wait()
            response = client.post("/api/configsync/4/migrate", json={"values": {}})
            outcomes.append(response.status_code)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes, [200] * 6)
        self.assertEqual(len(store.rows("configsync_staged")), 3)
        task_results = [
            row for row in store.rows("configsync_staged") if int(row["任务ID"]) == 4
        ]
        self.assertEqual(len(task_results), 3)

    def test_concurrent_stage_submission_applies_once(self) -> None:
        self.migrate(4)
        barrier = threading.Barrier(6)
        outcomes: list[bool] = []

        def worker() -> None:
            client = TestClient(app)
            barrier.wait()
            response = client.post(
                "/api/configsync/4/stages/1/actions",
                json={"values": {"action": "应用阶段"}},
            )
            outcomes.append(response.json()["ok"])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(all(outcomes))
        apply_audits = [
            row for row in store.rows("configsync_audit")
            if row["任务ID"] == 4 and row["动作"] == "应用阶段"
        ]
        self.assertEqual(len(apply_audits), 1)
        node_rows = [
            row for row in store.rows("node_config_versions")
            if row["节点编号"] == "WORK-0002" and row["配置版本号"] == "v3"
        ]
        self.assertEqual(len(node_rows), 1)

    def test_mapping_conflict_is_kept_pending(self) -> None:
        store.rows("configsync").append(
            {
                "id": 50,
                "status": "草稿",
                "pending": True,
                "abnormal": False,
                "任务编号": "CONF-0050",
                "源节点": "WORK-0001",
                "目标节点": "WORK-0002",
                "同步类型": "全量",
                "最后同步时间": "2026-09-10",
                "配置版本号": "v3",
                "差异摘要": "抓取规则",
                "同步状态": "草稿",
                "payload_version": "legacy-v1",
                "幂等标识": "sync:OTHER:v3",
            }
        )
        dataset = self.migrate(50)["entry"]["结果"]
        self.assertEqual([row["冲突类型"] for row in dataset["冲突记录"]], ["映射冲突"])
        response = self.apply(50, 1).json()
        self.assertFalse(response["ok"])
        self.assertIn("需先解决冲突", response["message"])
        terminal = self.client.get("/api/workterminal/nodes/WORK-0002/config").json()
        self.assertFalse(terminal["可下发"])

    def test_multiple_complete_versions_block_terminal(self) -> None:
        store.rows("node_config_versions").append(
            {
                "id": 80,
                "节点编号": "WORK-0002",
                "配置版本号": "v9",
                "来源任务": "CONF-0099",
                "来源任务ID": 99,
                "完整": True,
                "状态": "生效",
                "生效时间": "2026-10-01 10:00",
                "创建时间": "2026-10-01 10:00",
            }
        )
        terminal = self.client.get("/api/workterminal/nodes/WORK-0002/config").json()
        self.assertFalse(terminal["可下发"])
        self.assertEqual(terminal["已完整应用版本"]["配置版本号"], "v9")

    # -- 追溯与清单 -------------------------------------------------------
    def test_trace_links_task_version_and_audit(self) -> None:
        self.migrate(4)
        self.apply(4, 1)
        trace = self.client.get("/api/configsync/4/trace").json()
        self.assertEqual(trace["同步任务"]["任务编号"], "CONF-0004")
        self.assertIsNotNone(trace["分阶段同步结果"])
        self.assertTrue(trace["节点配置版本"])
        self.assertTrue(trace["同步审计结果"])
        lookup = self.client.get("/api/configsync/lookup", params={"task_no": "CONF-0004"}).json()
        self.assertEqual(lookup["total"], 1)
        self.assertEqual(lookup["items"][0]["追溯入口"], "/api/configsync/4/trace")

    def test_manifest_refresh_leaves_record(self) -> None:
        self.migrate(4)
        self.apply(4, 1)
        manifest = self.client.get("/api/workterminal/manifests/WORK-0002").json()
        self.assertEqual(manifest["已完整应用版本"]["配置版本号"], "v2")
        logs = self.client.get("/api/workterminal/manifests/WORK-0002/logs").json()
        self.assertGreaterEqual(logs["total"], 1)
        self.assertIn("结果摘要", manifest)

    def test_new_task_gets_explicit_version(self) -> None:
        response = self.client.post(
            "/api/configsync",
            json={"values": {"任务编号": "CONF-0100", "源节点": "WORK-0001", "目标节点": "WORK-0002"}},
        ).json()
        self.assertTrue(response["ok"])
        # WORK-0002 上已有 v2（节点）与 v3/v4（旧任务），新任务取下一个可用号。
        self.assertEqual(response["entry"]["配置版本号"], "v5")
        self.assertEqual(self.client.get(f"/api/configsync/{response['entry']['id']}").json()["载荷版本"], "staged-v2")

    def test_duplicate_task_number_is_rejected(self) -> None:
        response = self.client.post(
            "/api/configsync",
            json={"values": {"任务编号": "CONF-0001", "源节点": "WORK-0001", "目标节点": "WORK-0002"}},
        ).json()
        self.assertFalse(response["ok"])
        self.assertIn("已存在", response["message"])

    def test_legacy_confirm_records_node_version_once(self) -> None:
        first = self.client.post("/api/configsync/1/actions", json={"values": {"action": "确认完成"}}).json()
        self.assertTrue(first["ok"])
        node = first["entry"]["目标节点"]
        versions = [
            row for row in store.rows("node_config_versions") if row["节点编号"] == node
        ]
        self.assertEqual(len(versions), 1)
        node_versions_before = len(store.rows("node_config_versions"))
        audits_before = len(store.rows("configsync_audit"))
        self.client.post("/api/configsync/1/actions", json={"values": {"action": "确认完成"}})
        self.assertEqual(len(store.rows("node_config_versions")), node_versions_before)
        self.assertEqual(len(store.rows("configsync_audit")), audits_before)


if __name__ == "__main__":
    unittest.main()
