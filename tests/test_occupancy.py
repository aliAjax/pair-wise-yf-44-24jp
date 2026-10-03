import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, DomainError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, risk_occupancy
from src.service import DomainService


class OccupancyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.safety = Actor("safety-1", "safety")
        self.operator = Actor("op-1", "operator")
        self.verifier = Actor("ver-1", "verifier")
        self.engineer = Actor("eng-1", "engineer")
        self.viewer = Actor("viewer-1", "viewer")

    def tearDown(self):
        self.tmp.cleanup()

    # ----- fixtures -------------------------------------------------------

    def corridor(self, capacity=5):
        return self.service.create(
            self.admin, "corridor", {"name": "C", "capacity": capacity}
        )

    def unit(self, corridor_id=None, load=1):
        data = {"name": "U", "location": "Plant-A", "unit_occupancy": load}
        if corridor_id:
            data["corridor_id"] = corridor_id
        return self.service.create(self.admin, "unit", data)

    def approved_change(self, unit_id, risk, approvals=None):
        if approvals is None:
            approvals = ["S-%d" % i for i in range(risk_occupancy(risk))]
        change = self.service.create(
            self.admin, "change", {"unit_id": unit_id, "description": risk}
        )
        self.service.transition(
            self.admin, change["id"], "assess",
            {"risk_level": risk, "analyst": "E-1"},
        )
        self.service.transition(
            self.admin, change["id"], "approve",
            {"approvals": approvals, "permit_id": "MOC-1"},
        )
        return change

    def implement(self, change):
        return self.service.transition(
            self.admin, change["id"], "implement",
            {"procedure_version": "v1"},
        )

    # ----- risk-based capacity -------------------------------------------

    def test_risk_level_occupies_risk_weighted_capacity(self):
        self.assertEqual(risk_occupancy("low"), 1)
        self.assertEqual(risk_occupancy("medium"), 2)
        self.assertEqual(risk_occupancy("high"), 3)
        self.assertEqual(risk_occupancy("critical"), 4)
        corridor = self.corridor(capacity=3)
        unit = self.unit(corridor["id"])
        change = self.approved_change(unit["id"], "high")
        result = self.implement(change)
        self.assertEqual(result["status"], "implemented")
        view = self.service.occupancy(corridor["id"])
        self.assertEqual(view["used"], 3)
        self.assertEqual(view["available"], 0)

    def test_unassessed_change_cannot_implement(self):
        corridor = self.corridor()
        unit = self.unit(corridor["id"])
        change = self.service.create(
            self.admin, "change", {"unit_id": unit["id"], "description": "x"}
        )
        with self.assertRaises(DomainError):
            self.implement(change)

    # ----- queue ----------------------------------------------------------

    def test_change_queues_when_capacity_insufficient_and_promotes_on_release(self):
        corridor = self.corridor(capacity=3)
        unit = self.unit(corridor["id"])
        first = self.approved_change(unit["id"], "high")       # 3
        second = self.approved_change(unit["id"], "medium")    # 2
        self.assertEqual(self.implement(first)["status"], "implemented")
        self.assertEqual(self.implement(second)["status"], "queued")

        self.service.transition(
            self.admin, first["id"], "rollback", {"reason": "drift"}
        )
        self.assertEqual(self.service.get(second["id"])["status"], "implemented")
        view = self.service.occupancy(corridor["id"])
        self.assertEqual(view["used"], 2)
        self.assertEqual(view["waiting"], [])

    def test_queue_is_strict_fifo_head_of_line_blocks(self):
        corridor = self.corridor(capacity=5)
        unit = self.unit(corridor["id"])
        c0 = self.approved_change(unit["id"], "medium", ["S-1", "S-2"])  # 2
        c1 = self.approved_change(unit["id"], "high", ["S-1", "S-2", "S-3"])  # 3
        self.implement(c0)  # used 2
        self.implement(c1)  # used 5
        q1 = self.approved_change(unit["id"], "high", ["S-1", "S-2", "S-3"])  # 3
        q2 = self.approved_change(unit["id"], "low", ["S-1"])                # 1
        self.assertEqual(self.implement(q1)["status"], "queued")
        self.assertEqual(self.implement(q2)["status"], "queued")

        # Free enough for q2 but not for head q1: q2 must stay blocked.
        self.service.transition(
            self.admin, c0["id"], "rollback", {"reason": "r"}
        )
        self.assertEqual(self.service.get(q1["id"])["status"], "queued")
        self.assertEqual(self.service.get(q2["id"])["status"], "queued")

        # Free the rest: q1 then q2 promote in order.
        self.service.transition(
            self.admin, c1["id"], "rollback", {"reason": "r"}
        )
        self.assertEqual(self.service.get(q1["id"])["status"], "implemented")
        self.assertEqual(self.service.get(q2["id"])["status"], "implemented")

    def test_existing_waiters_force_new_change_to_queue_even_with_margin(self):
        corridor = self.corridor(capacity=5)
        unit = self.unit(corridor["id"])
        first = self.approved_change(unit["id"], "high", ["S-1", "S-2", "S-3"])  # 3
        second = self.approved_change(unit["id"], "high", ["S-1", "S-2", "S-3"])  # 3
        third = self.approved_change(unit["id"], "low", ["S-1"])                 # 1
        self.implement(first)   # used 3
        self.implement(second)  # queued (would overbook)
        third_result = self.implement(third)  # 3+1 fits but must wait behind
        self.assertEqual(third_result["status"], "queued")

    # ----- action items ---------------------------------------------------

    def test_action_item_occupies_until_verification_then_rearranges(self):
        corridor = self.corridor(capacity=4)
        unit = self.unit(corridor["id"])
        change = self.approved_change(unit["id"], "high", ["S-1", "S-2", "S-3"])  # 3
        self.implement(change)
        # Two open items take the remaining margin (4 - 3 = 1), so the next
        # low-risk change (1) must queue until re-inspection frees a slot.
        items = [
            self.service.create(
                self.safety,
                "action_item",
                {"change_id": change["id"], "description": "task-%d" % i,
                 "owner": "O-1"},
            )
            for i in range(2)
        ]
        waiting = self.approved_change(unit["id"], "low", ["S-1"])               # 1
        self.implement(waiting)
        self.assertEqual(self.service.get(waiting["id"])["status"], "queued")
        self.assertEqual(self.service.occupancy(corridor["id"])["used"], 5)

        # Commission stays blocked until the items are verified.
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "commission", {"tests_passed": True}
            )

        for item in items:
            self.service.transition(
                self.engineer, item["id"], "complete",
                {"completed_by": "O-1", "evidence": "log"},
            )
            self.service.transition(
                self.verifier, item["id"], "verify", {"verifier": "V-1"}
            )
        # After re-inspection the old item occupations are invalidated: the
        # queued change is promoted (used 3 + 1 = 4).
        self.assertEqual(self.service.get(waiting["id"])["status"], "implemented")
        self.assertEqual(self.service.occupancy(corridor["id"])["used"], 4)
    def test_action_item_reopen_reoccupies(self):
        corridor = self.corridor(capacity=3)
        unit = self.unit(corridor["id"])
        change = self.approved_change(unit["id"], "low", ["S-1"])
        self.implement(change)
        item = self.service.create(
            self.safety,
            "action_item",
            {"change_id": change["id"], "description": "train", "owner": "O-1"},
        )
        used_open = self.service.occupancy(corridor["id"])["used"]
        self.service.transition(
            self.engineer, item["id"], "complete",
            {"completed_by": "O-1", "evidence": "log"},
        )
        self.service.transition(
            self.verifier, item["id"], "verify", {"verifier": "V-1"}
        )
        self.service.transition(
            self.verifier, item["id"], "reopen", {"reason": "follow-up"}
        )
        self.assertEqual(
            self.service.occupancy(corridor["id"])["used"], used_open
        )

    # ----- unit shutdown / freeze ----------------------------------------

    def test_unit_shutdown_occupies_and_startup_invalidates_and_rearranges(self):
        corridor = self.corridor(capacity=5)
        unit = self.unit(corridor["id"], load=4)
        c0 = self.approved_change(unit["id"], "medium", ["S-1", "S-2"])  # 2
        c1 = self.approved_change(unit["id"], "high", ["S-1", "S-2", "S-3"])  # 3
        self.implement(c0)
        self.implement(c1)
        queued = self.approved_change(unit["id"], "high", ["S-1", "S-2", "S-3"])
        self.implement(queued)
        self.assertEqual(self.service.get(queued["id"])["status"], "queued")

        self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"}
        )
        self.assertEqual(self.service.occupancy(corridor["id"])["used"], 9)
        self.assertEqual(self.service.get(queued["id"])["status"], "queued")

        self.service.transition(
            self.operator, unit["id"], "startup", {"reason": "done"}
        )
        self.assertEqual(self.service.occupancy(corridor["id"])["used"], 5)
        self.assertEqual(self.service.get(queued["id"])["status"], "queued")

        # Release 3+2 on rollback: queue promotes (used 8 again).
        self.service.transition(self.admin, c0["id"], "rollback", {"reason": "r"})
        self.service.transition(self.admin, c1["id"], "rollback", {"reason": "r"})
        self.assertEqual(self.service.get(queued["id"])["status"], "implemented")

    def test_safety_freeze_voids_queued_occupations(self):
        corridor = self.corridor(capacity=2)
        unit = self.unit(corridor["id"])
        implemented = self.approved_change(unit["id"], "medium", ["S-1", "S-2"])
        self.implement(implemented)
        queued = self.approved_change(unit["id"], "low", ["S-1"])
        self.implement(queued)
        self.assertEqual(self.service.get(queued["id"])["status"], "queued")

        self.service.transition(
            self.safety, unit["id"], "freeze", {"reason": "hazard"}
        )
        # Unit itself occupies public capacity while frozen...
        self.assertEqual(self.service.occupancy(corridor["id"])["used"], 3)
        # ...but the not-yet-implemented queued change loses its place and is
        # sent back to approved; the waiting ledger row is voided.
        self.assertEqual(self.service.get(queued["id"])["status"], "approved")
        self.assertEqual(self.service.occupancy(corridor["id"])["waiting"], [])
        # Implemented occupations are not voided by a safety freeze.
        self.assertEqual(self.service.get(implemented["id"])["status"], "implemented")

    def test_operator_freeze_keeps_queue(self):
        corridor = self.corridor(capacity=1)
        unit = self.unit(corridor["id"])
        self.implement(self.approved_change(unit["id"], "low", ["S-1"]))
        queued = self.approved_change(unit["id"], "low", ["S-1"])
        self.implement(queued)
        self.service.transition(
            self.operator, unit["id"], "freeze", {"reason": "operational"}
        )
        self.assertEqual(self.service.get(queued["id"])["status"], "queued")

    def test_safety_freeze_then_reapply_and_unfreeze_rearranges(self):
        corridor = self.corridor(capacity=4)
        unit = self.unit(corridor["id"], load=4)
        change = self.approved_change(unit["id"], "medium", ["S-1", "S-2"])  # 2
        self.implement(change)  # used 2
        queued = self.approved_change(unit["id"], "high", ["S-1", "S-2", "S-3"])  # 3
        self.implement(queued)
        self.assertEqual(self.service.get(queued["id"])["status"], "queued")
        # Safety freeze: queued place is voided back to approved, unit occupies 4.
        self.service.transition(
            self.safety, unit["id"], "freeze", {"reason": "hazard"}
        )
        self.assertEqual(self.service.get(queued["id"])["status"], "approved")
        # Re-apply after freeze: queues behind the frozen unit (2+4+3 > 4).
        self.implement(queued)
        self.assertEqual(self.service.get(queued["id"])["status"], "queued")
        # Release the implemented change, then unfreeze: only then does the
        # queued change fit and get promoted.
        self.service.transition(
            self.admin, change["id"], "rollback", {"reason": "r"}
        )
        self.assertEqual(self.service.get(queued["id"])["status"], "queued")
        self.service.transition(
            self.safety, unit["id"], "unfreeze", {"reason": "cleared"}
        )
        self.assertEqual(self.service.get(queued["id"])["status"], "implemented")

    # ----- concurrency ----------------------------------------------------

    def test_concurrent_submits_same_corridor_only_one_wins(self):
        corridor = self.corridor(capacity=1)
        unit = self.unit(corridor["id"])
        changes = [
            self.approved_change(unit["id"], "low", ["S-1"]) for _ in range(2)
        ]
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def claim(change):
            try:
                barrier.wait()
                results.append(self.implement(change)["status"])
            except Exception as exc:  # pragma: no cover - surfaces failures
                errors.append(exc)

        threads = [threading.Thread(target=claim, args=(c,)) for c in changes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["implemented", "queued"])
        self.assertEqual(self.service.occupancy(corridor["id"])["used"], 1)

    # ----- idempotency ----------------------------------------------------

    def test_action_retry_is_idempotent_and_never_double_occupies(self):
        corridor = self.corridor(capacity=2)
        unit = self.unit(corridor["id"])
        change = self.approved_change(unit["id"], "low", ["S-1"])
        first = self.service.transition(
            self.admin, change["id"], "implement",
            {"procedure_version": "v1"}, idempotency_key="impl-1",
        )
        second = self.service.transition(
            self.admin, change["id"], "implement",
            {"procedure_version": "v1"}, idempotency_key="impl-1",
        )
        self.assertEqual(first["id"], second["id"])
        view = self.service.occupancy(corridor["id"])
        self.assertEqual(view["used"], 1)
        self.assertEqual(len(view["active"]), 1)

    def test_queued_retry_replays_queued_result(self):
        corridor = self.corridor(capacity=1)
        unit = self.unit(corridor["id"])
        first = self.approved_change(unit["id"], "low", ["S-1"])
        second = self.approved_change(unit["id"], "low", ["S-1"])
        self.implement(first)
        result = self.service.transition(
            self.admin, second["id"], "implement",
            {"procedure_version": "v1"}, idempotency_key="impl-queue",
        )
        self.assertEqual(result["status"], "queued")
        replay = self.service.transition(
            self.admin, second["id"], "implement",
            {"procedure_version": "v1"}, idempotency_key="impl-queue",
        )
        self.assertEqual(replay["status"], "queued")
        self.assertEqual(
            len(self.service.occupancy(corridor["id"])["waiting"]), 1
        )

    def test_retried_request_after_auto_promotion_does_not_reoccupy(self):
        corridor = self.corridor(capacity=1)
        unit = self.unit(corridor["id"])
        first = self.approved_change(unit["id"], "low", ["S-1"])
        second = self.approved_change(unit["id"], "low", ["S-1"])
        self.implement(first)
        self.assertEqual(
            self.service.transition(
                self.admin, second["id"], "implement",
                {"procedure_version": "v1"}, idempotency_key="retry-1",
            )["status"],
            "queued",
        )
        # The client never got the response. Capacity frees, queue promotes.
        self.service.transition(
            self.admin, first["id"], "rollback", {"reason": "r"}
        )
        self.assertEqual(self.service.get(second["id"])["status"], "implemented")
        # Client retries with the same key: gets the promoted entity, and the
        # ledger still holds exactly one occupation for it.
        replay = self.service.transition(
            self.admin, second["id"], "implement",
            {"procedure_version": "v1"}, idempotency_key="retry-1",
        )
        self.assertEqual(replay["status"], "implemented")
        view = self.service.occupancy(corridor["id"])
        self.assertEqual(view["used"], 1)
        self.assertEqual(len(view["active"]), 1)

    # ----- authorization --------------------------------------------------

    def test_unauthorized_actor_is_rejected(self):
        corridor = self.corridor()
        unit = self.unit(corridor["id"])
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.viewer, unit["id"], "shutdown", {"reason": "x"}
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.viewer, unit["id"], "freeze", {"reason": "x"}
            )
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.viewer, "corridor", {"name": "C2", "capacity": 1}
            )
        # Unit stays operating after rejected attempts.
        self.assertEqual(self.service.get(unit["id"])["status"], "operating")

    # ----- ledger consistency --------------------------------------------

    def test_old_occupations_are_voided_not_overwritten(self):
        corridor = self.corridor(capacity=2)
        unit = self.unit(corridor["id"], load=2)
        self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "m"}
        )
        self.service.transition(
            self.operator, unit["id"], "startup", {"reason": "d"}
        )
        rows = self.repo.list_occupancy(corridor["id"])
        statuses = [(row["holder_type"], row["status"], row["release_reason"])
                    for row in rows]
        # Append-only ledger: the shutdown row stays on record marked released;
        # it is never overwritten in place.
        self.assertEqual(
            statuses,
            [("unit", "released", "unit_startup")],
        )
        self.assertEqual(self.service.occupancy(corridor["id"])["used"], 0)

    def test_corridor_validation(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin, "corridor", {"name": "C", "capacity": -1}
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin, "unit",
                {"name": "U", "location": "P", "corridor_id": "missing"},
            )

    def test_action_item_requires_existing_change(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.safety,
                "action_item",
                {"change_id": "nope", "description": "d", "owner": "o"},
            )


if __name__ == "__main__":
    unittest.main()
