import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class OccupancyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("eng", "engineer")
        self.safety = Actor("safe", "safety")

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # 基础：变更按风险等级占容量
    # ------------------------------------------------------------------
    def test_change_occupies_by_risk_level(self):
        unit = self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        self.service.set_zone_capacity(self.admin, "Plant-A", 10)

        low = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "low"})
        self.service.transition(self.admin, low["id"], "assess", {"risk_level": "low", "analyst": "E"})
        self.service.transition(self.admin, low["id"], "approve", {"approvals": ["a"], "permit_id": "p"})
        self.service.transition(self.admin, low["id"], "implement", {"procedure_version": "v1"})

        high = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "high"})
        self.service.transition(self.admin, high["id"], "assess", {"risk_level": "high", "analyst": "E"})
        self.service.transition(self.admin, high["id"], "approve", {"approvals": ["a", "b", "c"], "permit_id": "p"})
        self.service.transition(self.admin, high["id"], "implement", {"procedure_version": "v1"})

        status = self.service.zone_status("Plant-A")
        self.assertEqual(status["occupied"], 1 + 3)
        self.assertEqual(status["available"], 6)
        amounts = {o["owner_id"]: o["amount"] for o in status["active"]}
        self.assertEqual(amounts[low["id"]], 1)
        self.assertEqual(amounts[high["id"]], 3)

    # ------------------------------------------------------------------
    # 容量不够就排队
    # ------------------------------------------------------------------
    def test_queue_when_insufficient_capacity(self):
        unit = self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        self.service.set_zone_capacity(self.admin, "Plant-A", 5)

        high = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "high"})
        self.service.transition(self.admin, high["id"], "assess", {"risk_level": "high", "analyst": "E"})
        self.service.transition(self.admin, high["id"], "approve", {"approvals": ["a", "b", "c"], "permit_id": "p"})
        high = self.service.transition(self.admin, high["id"], "implement", {"procedure_version": "v1"})
        self.assertEqual(high["status"], "implemented")

        critical = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "critical"})
        self.service.transition(self.admin, critical["id"], "assess", {"risk_level": "critical", "analyst": "E"})
        self.service.transition(self.admin, critical["id"], "approve", {"approvals": ["a", "b", "c", "d"], "permit_id": "p"})
        critical = self.service.transition(self.admin, critical["id"], "implement", {"procedure_version": "v1"})
        self.assertEqual(critical["status"], "queued")

        medium = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "medium"})
        self.service.transition(self.admin, medium["id"], "assess", {"risk_level": "medium", "analyst": "E"})
        self.service.transition(self.admin, medium["id"], "approve", {"approvals": ["a", "b"], "permit_id": "p"})
        medium = self.service.transition(self.admin, medium["id"], "implement", {"procedure_version": "v1"})
        self.assertEqual(medium["status"], "implemented")

        status = self.service.zone_status("Plant-A")
        self.assertEqual(status["occupied"], 3 + 2)
        self.assertEqual(status["available"], 0)
        self.assertEqual(len(status["queue"]), 1)
        self.assertEqual(status["queue"][0]["owner_id"], critical["id"])
        self.assertEqual(status["queue"][0]["status"], "queued")

    # ------------------------------------------------------------------
    # 装置恢复后旧占用失效并重排
    # ------------------------------------------------------------------
    def test_rearrange_on_unit_recovery(self):
        unit = self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        self.service.set_zone_capacity(self.admin, "Plant-A", 5)

        # 装置停车占 2
        self.service.transition(self.admin, unit["id"], "shutdown", {"reason": "maintenance"})
        # 高风险变更占 3 -> 余量 0
        high = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "high"})
        self.service.transition(self.admin, high["id"], "assess", {"risk_level": "high", "analyst": "E"})
        self.service.transition(self.admin, high["id"], "approve", {"approvals": ["a", "b", "c"], "permit_id": "p"})
        self.service.transition(self.admin, high["id"], "implement", {"procedure_version": "v1"})
        # 中风险变更排队
        medium = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "medium"})
        self.service.transition(self.admin, medium["id"], "assess", {"risk_level": "medium", "analyst": "E"})
        self.service.transition(self.admin, medium["id"], "approve", {"approvals": ["a", "b"], "permit_id": "p"})
        medium = self.service.transition(self.admin, medium["id"], "implement", {"procedure_version": "v1"})
        self.assertEqual(medium["status"], "queued")

        # 装置恢复 -> 释放占用并重排，队列中的 medium 被激活并自动实施
        self.service.transition(self.admin, unit["id"], "startup", {"reason": "done"})
        status = self.service.zone_status("Plant-A")
        self.assertEqual(len(status["queue"]), 0)
        self.assertEqual(status["occupied"], 3 + 2)  # high + medium
        self.assertEqual(status["available"], 0)
        medium = self.service.get(medium["id"])
        self.assertEqual(medium["status"], "implemented")

    # ------------------------------------------------------------------
    # 行动项复验后旧占用失效并重排
    # ------------------------------------------------------------------
    def test_rearrange_on_action_item_verify(self):
        unit = self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        self.service.set_zone_capacity(self.admin, "Plant-A", 5)

        high = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "high"})
        self.service.transition(self.admin, high["id"], "assess", {"risk_level": "high", "analyst": "E"})
        self.service.transition(self.admin, high["id"], "approve", {"approvals": ["a", "b", "c"], "permit_id": "p"})
        self.service.transition(self.admin, high["id"], "implement", {"procedure_version": "v1"})

        # 行动项占 1 -> 余量 1
        item = self.service.create(self.admin, "action_item", {"change_id": high["id"], "description": "measure", "owner": "O-1"})
        status = self.service.zone_status("Plant-A")
        self.assertEqual(status["occupied"], 3 + 1)
        self.assertEqual(status["available"], 1)

        # 中风险变更排队（余量 1 < 2）
        medium = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "medium"})
        self.service.transition(self.admin, medium["id"], "assess", {"risk_level": "medium", "analyst": "E"})
        self.service.transition(self.admin, medium["id"], "approve", {"approvals": ["a", "b"], "permit_id": "p"})
        medium = self.service.transition(self.admin, medium["id"], "implement", {"procedure_version": "v1"})
        self.assertEqual(medium["status"], "queued")

        # 行动项复验 -> 释放占用并重排，medium 被激活
        self.service.transition(self.admin, item["id"], "complete", {"completed_by": "O-1", "evidence": "log"})
        self.service.transition(self.admin, item["id"], "verify", {"verifier": "V-1"})
        status = self.service.zone_status("Plant-A")
        self.assertEqual(len(status["queue"]), 0)
        self.assertEqual(status["occupied"], 3 + 2)
        medium = self.service.get(medium["id"])
        self.assertEqual(medium["status"], "implemented")

    # ------------------------------------------------------------------
    # 写入失败后重试不重复占用
    # ------------------------------------------------------------------
    def test_retry_after_failure_no_double_occupy(self):
        unit = self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        self.service.set_zone_capacity(self.admin, "Plant-A", 10)
        change = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "c"})
        self.service.transition(self.admin, change["id"], "assess", {"risk_level": "medium", "analyst": "E"})
        self.service.transition(self.admin, change["id"], "approve", {"approvals": ["a", "b"], "permit_id": "p"})

        # 第一次带错误版本 -> 冲突，事务内核对余量前就回滚，不产生占用
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, change["id"], "implement", {"procedure_version": "v1"}, expected_version=999)
        self.assertEqual(len(self.service.list_occupancies(zone="Plant-A")), 0)

        # 重试成功 -> 只放一份
        self.service.transition(self.admin, change["id"], "implement", {"procedure_version": "v1"})
        self.assertEqual(len(self.service.list_occupancies(zone="Plant-A")), 1)
        status = self.service.zone_status("Plant-A")
        self.assertEqual(status["occupied"], 2)
        self.assertEqual(status["available"], 8)

    def test_idempotent_retry_returns_same_result(self):
        unit = self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        self.service.set_zone_capacity(self.admin, "Plant-A", 10)
        change = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "c"})
        self.service.transition(self.admin, change["id"], "assess", {"risk_level": "low", "analyst": "E"})
        self.service.transition(self.admin, change["id"], "approve", {"approvals": ["a"], "permit_id": "p"})

        first = self.service.transition(self.admin, change["id"], "implement", {"procedure_version": "v1"}, idempotency_key="impl-1")
        second = self.service.transition(self.admin, change["id"], "implement", {"procedure_version": "v1"}, idempotency_key="impl-1")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.service.list_occupancies(zone="Plant-A")), 1)

    # ------------------------------------------------------------------
    # 同区同时提交只放一份（并发）
    # ------------------------------------------------------------------
    def test_concurrent_implement_same_change_one_occupancy(self):
        unit = self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        self.service.set_zone_capacity(self.admin, "Plant-A", 10)
        change = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "c"})
        self.service.transition(self.admin, change["id"], "assess", {"risk_level": "medium", "analyst": "E"})
        self.service.transition(self.admin, change["id"], "approve", {"approvals": ["a", "b"], "permit_id": "p"})

        barrier = threading.Barrier(6)
        errors = []

        def worker():
            barrier.wait()
            try:
                self.service.transition(self.admin, change["id"], "implement", {"procedure_version": "v1"})
            except (ConflictError, InvalidTransition):
                # 竞争失败：版本冲突，或变更单已被对方置为 implemented
                errors.append("lost")

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        occs = [o for o in self.service.list_occupancies(zone="Plant-A") if o["owner_id"] == change["id"]]
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0]["status"], "active")
        change = self.service.get(change["id"])
        self.assertEqual(change["status"], "implemented")

    # ------------------------------------------------------------------
    # 安全员冻结后未实施占用失效
    # ------------------------------------------------------------------
    def test_safety_freeze_invalidates_unimplemented_occupancy(self):
        unit = self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        self.service.set_zone_capacity(self.admin, "Plant-A", 5)

        high = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "high"})
        self.service.transition(self.admin, high["id"], "assess", {"risk_level": "high", "analyst": "E"})
        self.service.transition(self.admin, high["id"], "approve", {"approvals": ["a", "b", "c"], "permit_id": "p"})
        self.service.transition(self.admin, high["id"], "implement", {"procedure_version": "v1"})

        critical = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "critical"})
        self.service.transition(self.admin, critical["id"], "assess", {"risk_level": "critical", "analyst": "E"})
        self.service.transition(self.admin, critical["id"], "approve", {"approvals": ["a", "b", "c", "d"], "permit_id": "p"})
        self.service.transition(self.admin, critical["id"], "implement", {"procedure_version": "v1"})
        critical = self.service.get(critical["id"])
        self.assertEqual(critical["status"], "queued")

        # 安全员冻结 critical -> 未实施的排队占用失效，变更冻结
        frozen = self.service.transition(self.safety, critical["id"], "freeze", {"reason": "safety hold"})
        self.assertEqual(frozen["status"], "frozen")
        occs = self.service.list_occupancies(zone="Plant-A", status="invalid")
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0]["owner_id"], critical["id"])
        # 队列中不再有 critical
        status = self.service.zone_status("Plant-A")
        self.assertEqual(len(status["queue"]), 0)

        # 解冻后回到 approved，可重新申请实施
        unfrozen = self.service.transition(self.safety, critical["id"], "unfreeze", {"reason": "resume"})
        self.assertEqual(unfrozen["status"], "approved")

    # ------------------------------------------------------------------
    # 越权被拒
    # ------------------------------------------------------------------
    def test_unauthorized_freeze_rejected(self):
        unit = self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        change = self.service.create(self.admin, "change", {"unit_id": unit["id"], "description": "c"})
        self.service.transition(self.admin, change["id"], "assess", {"risk_level": "low", "analyst": "E"})
        self.service.transition(self.admin, change["id"], "approve", {"approvals": ["a"], "permit_id": "p"})
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.engineer, change["id"], "freeze", {"reason": "x"})

    def test_set_capacity_requires_admin(self):
        unit = self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        with self.assertRaises(PermissionDenied):
            self.service.set_zone_capacity(self.engineer, "Plant-A", 10)

    # ------------------------------------------------------------------
    # 分区查询
    # ------------------------------------------------------------------
    def test_list_zones_and_zone_status(self):
        self.service.create(self.admin, "unit", {"name": "U-1", "location": "Plant-A"})
        self.service.create(self.admin, "unit", {"name": "U-2", "location": "Plant-B"})
        self.service.set_zone_capacity(self.admin, "Plant-A", 20)
        zones = {z["zone"]: z for z in self.service.list_zones()}
        self.assertIn("Plant-A", zones)
        self.assertIn("Plant-B", zones)
        self.assertEqual(zones["Plant-A"]["capacity"], 20)
        self.assertEqual(zones["Plant-B"]["capacity"], 10)  # 默认容量


if __name__ == "__main__":
    unittest.main()
