from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .repository import utcnow
from .rules import (
    ACTION_ITEM_OCCUPANCY,
    DEFAULT_UNIT_OCCUPANCY,
    RuleEngine,
    risk_occupancy,
)


class DomainService:
    """Use-case orchestration around the occupancy ledger.

    Every write that can touch capacity runs inside one repository
    transaction (BEGIN IMMEDIATE), so concurrent claims on the same
    corridor are serialised and the ledger can never be double-charged.
    """

    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    # ----- helpers --------------------------------------------------------

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _lookup_conn(self, conn, kind, field, value):
        return self.repository.find_entities_conn(
            conn, self.rules.normalize_kind(kind), field, value
        )

    def _get_conn(self, conn, entity_id):
        return self.repository._get_entity_conn(conn, entity_id)

    def _audit_conn(self, conn, entity_id, actor, action, from_status,
                    to_status, detail=None):
        self.repository._append_audit_conn(
            conn,
            entity_id,
            actor.user_id,
            actor.role,
            action,
            from_status,
            to_status,
            detail or {},
        )

    def _corridor_of_unit(self, conn, unit):
        return unit["data"].get("corridor_id")

    def _corridor_of_change(self, conn, change):
        unit = self._get_conn(conn, change["data"].get("unit_id"))
        return self._corridor_of_unit(conn, unit) if unit else None

    def _corridor_of_item(self, conn, item):
        change = self._get_conn(conn, item["data"].get("change_id"))
        return self._corridor_of_change(conn, change) if change else None

    def _corridor_capacity(self, conn, corridor_id):
        corridor = self._get_conn(conn, corridor_id)
        if not corridor or self.rules.normalize_kind(corridor["kind"]) != "corridor":
            return None
        return int(corridor["data"]["capacity"])

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ----- create ---------------------------------------------------------

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        with self.repository.transaction() as conn:
            if idempotency_key:
                existing = self.repository._get_idempotency_conn(
                    conn, actor.user_id, idempotency_key, "create"
                )
                if existing:
                    entity = self.repository._get_entity_conn(conn, existing["entity_id"])
                    if entity:
                        return entity
            # Rules are checked inside the write transaction so that the
            # corridor/unit existence view cannot change between check and write.
            self.rules.validate_create(
                actor, kind, payload,
                lambda k, f, v: self._lookup_conn(conn, k, f, v),
            )
            entity_id = str(payload.pop("id", "") or uuid4())
            if self.repository._get_entity_conn(conn, entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            status = self.rules.initial_status(kind)
            self.repository._insert_entity_conn(
                conn, entity_id, kind, status, payload, actor.user_id
            )
            created = self.repository._get_entity_conn(conn, entity_id)
            self._audit_conn(conn, entity_id, actor, "create", None, status,
                             {"kind": kind})

            # Opening an action item immediately reserves its corridor slot.
            if kind == "action_item":
                self._open_item_occupancy(conn, created)

            if idempotency_key:
                self.repository._save_idempotency_conn(
                    conn, actor.user_id, idempotency_key, "create",
                    entity_id, status,
                )
        return self.repository.get_entity(entity_id)

    # ----- occupancy events ----------------------------------------------

    def _open_item_occupancy(self, conn, item):
        corridor_id = self._corridor_of_item(conn, item)
        if not corridor_id:
            return
        self.repository._insert_occupancy_conn(
            conn, corridor_id, "action_item", item["id"],
            ACTION_ITEM_OCCUPANCY, "action_item_open", "active",
        )

    def _release_holder(self, conn, holder_type, holder_id, reason, corridor_id=None):
        self.repository._release_occupancy_conn(
            conn, holder_type, holder_id, reason, corridor_id=corridor_id
        )

    def _drain_queue(self, conn):
        """Recompute: promote queued changes in strict FIFO order while
        remaining corridor capacity allows. Head-of-line blocking applies:
        the first change that does not fit stops its corridor's promotion."""
        promoted = []
        # Gather corridors that currently have waiters.
        rows = conn.execute(
            "SELECT DISTINCT corridor_id FROM occupancy_ledger WHERE status = 'waiting'"
        ).fetchall()
        for row in rows:
            corridor_id = row["corridor_id"]
            capacity = self._corridor_capacity(conn, corridor_id)
            if capacity is None:
                continue
            self._drain_corridor(conn, corridor_id, capacity, promoted)
        return promoted

    def _drain_corridor(self, conn, corridor_id, capacity, promoted):
        while True:
            waiting = self.repository._list_occupancy_conn(
                conn, corridor_id, status="waiting"
            )
            if not waiting:
                return
            head = waiting[0]
            change = self._get_conn(conn, head["holder_id"])
            # Waiter may reference a change that left the queue (e.g. freeze
            # cancelled it). Drop the stale row and keep draining.
            if not change or change["status"] != self.rules.QUEUEABLE_STATUS:
                self.repository._cancel_waiting_conn(
                    conn, "change", head["holder_id"], "stale_waiter",
                    corridor_id=corridor_id,
                )
                continue
            used = self.repository._active_usage_conn(conn, corridor_id)
            amount = int(head["amount"])
            if not self.rules.fits(capacity, used, amount):
                return  # head of line blocks later changes
            self.repository._cancel_waiting_conn(
                conn, "change", change["id"], "capacity_available",
                corridor_id=corridor_id,
            )
            self.repository._insert_occupancy_conn(
                conn, corridor_id, "change", change["id"], amount,
                "change_implemented", "active",
            )
            new_data = dict(change["data"])
            new_data["implemented_at"] = utcnow()
            self.repository._update_entity_conn(
                conn, change["id"], change["version"],
                "implemented", new_data,
            )
            self._audit_conn(
                conn, change["id"], _SYSTEM_ACTOR,
                self.rules.PROMOTE_ACTION, "queued", "implemented",
                {"amount": amount, "corridor_id": corridor_id},
            )
            promoted.append(change["id"])

    # ----- transitions ----------------------------------------------------

    def transition(self, actor, entity_id, action, data=None,
                   expected_version=None, idempotency_key=None):
        with self.repository.transaction() as conn:
            if idempotency_key:
                existing = self.repository._get_idempotency_conn(
                    conn, actor.user_id, idempotency_key, "action"
                )
                if existing:
                    # The original request already completed; replay the
                    # entity's current version (it may have moved on, e.g.
                    # queued -> implemented via auto-promotion).
                    entity = self.repository._get_entity_conn(conn, existing["entity_id"])
                    if entity:
                        return entity
            entity = self.repository._get_entity_conn(conn, entity_id)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
            expected = (int(expected_version)
                        if expected_version is not None
                        else entity["version"])
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}),
                lambda k, f, v: self._lookup_conn(conn, k, f, v),
            )

            kind = self.rules.normalize_kind(entity["kind"])
            actual_status = next_status
            promoted = []

            if kind == "change" and action == "implement":
                actual_status = self._do_implement(conn, actor, entity, patch, expected)
                self._audit_conn(
                    conn, entity_id, actor, action,
                    entity["status"], actual_status,
                    {"patch": patch},
                )
                if actual_status == self.rules.QUEUEABLE_STATUS:
                    result = self.repository._get_entity_conn(conn, entity_id)
                    if idempotency_key:
                        self.repository._save_idempotency_conn(
                            conn, actor.user_id, idempotency_key, "action",
                            entity_id, actual_status,
                        )
                    return result
                promoted = self._drain_queue(conn)
            else:
                merged = dict(entity["data"])
                merged.update(patch)
                self.repository._update_entity_conn(
                    conn, entity_id, expected, next_status, merged
                )
                promoted = self._apply_occupancy_event(
                    conn, actor, entity, action, next_status, merged
                )

            self._audit_conn(
                conn, entity_id, actor, action,
                entity["status"],
                self.repository._get_entity_conn(conn, entity_id)["status"],
                {"patch": patch, "auto_promoted": promoted},
            )
            result = self.repository._get_entity_conn(conn, entity_id)
            if idempotency_key:
                self.repository._save_idempotency_conn(
                    conn, actor.user_id, idempotency_key, "action",
                    entity_id, result["status"],
                )
        return self.repository.get_entity(entity_id)

    def _do_implement(self, conn, actor, change, patch, expected_version):
        """Pre-implementation margin check: occupy or queue. Strict FIFO:
        if the corridor already has waiters, the new change queues behind
        them even if some margin momentarily exists."""
        corridor_id = self._corridor_of_change(conn, change)
        amount = risk_occupancy(change["data"].get("risk_level"))
        queued = False
        if corridor_id:
            capacity = self._corridor_capacity(conn, corridor_id)
            has_waiters = self.repository._has_waiting_conn(conn, corridor_id)
            used = self.repository._active_usage_conn(conn, corridor_id)
            queued = has_waiters or not self.rules.fits(capacity, used, amount)
            if queued:
                self.repository._insert_occupancy_conn(
                    conn, corridor_id, "change", change["id"], amount,
                    "change_waiting", "waiting", queued_at=utcnow(),
                )
            else:
                self.repository._insert_occupancy_conn(
                    conn, corridor_id, "change", change["id"], amount,
                    "change_implemented", "active",
                )
        merged = dict(change["data"])
        merged.update(patch)
        status = self.rules.QUEUEABLE_STATUS if queued else "implemented"
        if not queued:
            merged["implemented_at"] = utcnow()
        self.repository._update_entity_conn(
            conn, change["id"], expected_version, status, merged
        )
        return status

    def _apply_occupancy_event(self, conn, actor, entity, action, next_status,
                               merged_data):
        """Translate a state change into ledger entries, return auto-promoted
        change ids after any released capacity is rearranged."""
        kind = self.rules.normalize_kind(entity["kind"])

        if kind == "unit":
            return self._unit_event(conn, actor, entity, action)

        if kind == "change":
            return self._change_event(conn, actor, entity, action)

        if kind == "action_item":
            return self._item_event(conn, actor, entity, action)

        return []

    def _unit_event(self, conn, actor, entity, action):
        corridor_id = self._corridor_of_unit(conn, entity)
        promoted = []
        if action in ("shutdown", "freeze"):
            # Unit leaves normal operation: its old (zero) occupation is
            # recomputed into a public-capacity reservation.
            if corridor_id:
                load = int(entity["data"].get("unit_occupancy", DEFAULT_UNIT_OCCUPANCY))
                self.repository._insert_occupancy_conn(
                    conn, corridor_id, "unit", entity["id"], load,
                    "unit_" + action, "active",
                )
            # Safety-officer freeze voids every not-yet-implemented occupation
            # of changes under this unit (queued place-holders). They return to
            # approved and must re-apply for capacity.
            if action == "freeze" and actor.role == "safety":
                for change in self._lookup_conn(conn, "change", "unit_id", entity["id"]):
                    if change["status"] == self.rules.QUEUEABLE_STATUS:
                        cancelled = self.repository._cancel_waiting_conn(
                            conn, "change", change["id"], "safety_freeze",
                            corridor_id=corridor_id,
                        )
                        if cancelled:
                            self.repository._update_entity_conn(
                                conn, change["id"], change["version"],
                                "approved", change["data"],
                            )
                            self._audit_conn(
                                conn, change["id"], actor, "queue_cancelled",
                                "queued", "approved",
                                {"reason": "safety_freeze", "unit_id": entity["id"]},
                            )
            if corridor_id:
                promoted = self._drain_queue(conn)
        elif action in ("startup", "unfreeze"):
            # Unit recovered: its shutdown/freeze occupation is invalidated
            # and the queue is rearranged.
            if corridor_id:
                self._release_holder(conn, "unit", entity["id"],
                                     "unit_" + action, corridor_id=corridor_id)
                promoted = self._drain_queue(conn)
        return promoted

    def _change_event(self, conn, actor, entity, action):
        corridor_id = self._corridor_of_change(conn, entity)
        if action == "commission":
            # Change handed over to production: its temporary occupation ends.
            if corridor_id:
                self._release_holder(conn, "change", entity["id"],
                                     "change_commissioned",
                                     corridor_id=corridor_id)
                return self._drain_queue(conn)
        if action in ("rollback", "close"):
            if corridor_id:
                self._release_holder(conn, "change", entity["id"],
                                     "change_" + action,
                                     corridor_id=corridor_id)
                return self._drain_queue(conn)
        return []

    def _item_event(self, conn, actor, entity, action):
        corridor_id = self._corridor_of_item(conn, entity)
        if not corridor_id:
            return []
        if action == "verify":
            # Re-inspection passed: the old open/completed occupation is
            # invalidated and capacity is rearranged.
            self._release_holder(conn, "action_item", entity["id"],
                                 "item_verified", corridor_id=corridor_id)
            return self._drain_queue(conn)
        if action == "reopen":
            self.repository._insert_occupancy_conn(
                conn, corridor_id, "action_item", entity["id"],
                ACTION_ITEM_OCCUPANCY, "action_item_reopened", "active",
            )
            return self._drain_queue(conn)
        return []

    # ----- reads ----------------------------------------------------------

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def occupancy(self, corridor_id):
        """Occupation account of one corridor."""
        with self.repository.transaction() as conn:
            corridor = self.repository._get_entity_conn(conn, corridor_id)
            if not corridor or self.rules.normalize_kind(corridor["kind"]) != "corridor":
                raise NotFoundError("corridor not found: " + corridor_id)
            capacity = int(corridor["data"]["capacity"])
            used = self.repository._active_usage_conn(conn, corridor_id)
            waiting = self.repository._list_occupancy_conn(
                conn, corridor_id, status="waiting"
            )
            active = self.repository._list_occupancy_conn(
                conn, corridor_id, status="active"
            )
        return {
            "corridor_id": corridor_id,
            "capacity": capacity,
            "used": used,
            "available": capacity - used,
            "active": [self._occupancy_view(row) for row in active],
            "waiting": [self._occupancy_view(row) for row in waiting],
        }

    @staticmethod
    def _occupancy_view(row):
        return {
            "id": row["id"],
            "holder_type": row["holder_type"],
            "holder_id": row["holder_id"],
            "amount": int(row["amount"]),
            "reason": row["reason"],
            "status": row["status"],
            "queued_at": row["queued_at"],
            "created_at": row["created_at"],
        }

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)


class _SystemActor:
    user_id = "system"
    role = "admin"


_SYSTEM_ACTOR = _SystemActor()
