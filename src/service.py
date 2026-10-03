from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (
    ACTION_ITEM_OCCUPANCY,
    UNIT_OCCUPANCY,
    RuleEngine,
    change_occupancy,
)


class DomainService:
    # (kind, action) -> (操作类型, 占用属主, 是否强制)
    #   occupy: 核对余量后占用（不足则排队）；force=True 时强制占用（装置停车/冻结）
    #   release: 释放占用并重排队列；invalidate_queued=True 时仅失效未实施的排队占用
    OCCUPANCY_ACTIONS = {
        ("unit", "shutdown"): ("occupy", "unit", True),
        ("unit", "freeze"): ("occupy", "unit", True),
        ("unit", "startup"): ("release", "unit", False),
        ("unit", "unfreeze"): ("release", "unit", False),
        ("change", "implement"): ("occupy", "change", False),
        ("change", "rollback"): ("release", "change", False),
        ("change", "freeze"): ("release", "change", True),
        ("action_item", "verify"): ("release", "action_item", False),
        ("action_item", "reopen"): ("occupy", "action_item", False),
    }

    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _find_one(self, kind, field, value):
        rows = self._lookup(kind, field, value) or []
        return rows[0] if rows else None

    def _zone_for_data(self, kind, data):
        """解析对象所属管廊分区：装置取 zone/location，变更/行动项沿装置归属。"""
        if kind == "unit":
            return data.get("zone") or data.get("location")
        if kind == "change":
            unit = self._find_one("unit", "id", data.get("unit_id"))
            if not unit:
                raise ValidationError("unit does not exist")
            return unit.get("data", {}).get("zone") or unit.get("data", {}).get("location")
        if kind == "action_item":
            change = self._find_one("change", "id", data.get("change_id"))
            if not change:
                raise ValidationError("change does not exist")
            return self._zone_for_data("change", change.get("data", {}))
        raise ValidationError("cannot resolve zone for " + str(kind))

    def _zone_for_entity(self, kind, entity):
        return self._zone_for_data(kind, entity.get("data", {}))

    def _occupy_params(self, kind, entity, merged):
        """返回 (zone, amount)。装置固定当量，变更按风险等级，行动项固定当量。"""
        zone = self._zone_for_entity(kind, entity)
        if kind == "unit":
            return zone, UNIT_OCCUPANCY
        if kind == "change":
            return zone, change_occupancy(merged.get("risk_level"))
        if kind == "action_item":
            return zone, ACTION_ITEM_OCCUPANCY
        raise ValidationError("cannot occupy for " + str(kind))

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        # 行动项创建即占用（未复验期间占容量），先解析分区再建单
        occupy = kind == "action_item"
        if occupy:
            zone = self._zone_for_data(kind, payload)
            amount = ACTION_ITEM_OCCUPANCY
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if occupy:
            occ_status, entity = self.repository.occupy_and_transition(
                entity_id, 1, status, payload, zone, "action_item", amount, force=False
            )
            self.audit.record(
                entity_id, actor, "occupy", None, status,
                {"zone": zone, "amount": amount, "occupancy": occ_status},
            )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None,
                   idempotency_key=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                found = self.repository.get_entity(existing)
                if found:
                    return found
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        kind = self.rules.normalize_kind(entity["kind"])
        dispatch = self.OCCUPANCY_ACTIONS.get((kind, action))
        if dispatch:
            op, owner_kind, flag = dispatch
            if op == "occupy":
                zone, amount = self._occupy_params(kind, entity, merged)
                occ_status, updated = self.repository.occupy_and_transition(
                    entity_id, expected, next_status, merged, zone, owner_kind, amount, force=flag
                )
                self.audit.record(
                    entity_id, actor, action, entity["status"], updated["status"],
                    {"patch": patch, "zone": zone, "amount": amount, "occupancy": occ_status},
                )
            else:
                updated = self.repository.release_and_transition(
                    entity_id, expected, next_status, merged, owner_kind, invalidate_queued=flag
                )
                self.audit.record(
                    entity_id, actor, action, entity["status"], updated["status"],
                    {"patch": patch},
                )
        else:
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)
            self.audit.record(
                entity_id, actor, action, entity["status"], updated["status"],
                {"patch": patch},
            )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # 占用账 / 管廊分区
    # ------------------------------------------------------------------

    def list_occupancies(self, zone=None, status=None):
        return self.repository.list_occupancies(zone=zone, status=status)

    def get_occupancy(self, occupancy_id):
        occupancy = self.repository.get_occupancy(occupancy_id)
        if not occupancy:
            raise NotFoundError("occupancy not found: " + occupancy_id)
        return occupancy

    def zone_status(self, zone):
        return self.repository.zone_status(zone)

    def list_zones(self):
        return self.repository.list_zones()

    def set_zone_capacity(self, actor, zone, capacity):
        self.rules._ensure_role(actor, ("admin",))
        try:
            capacity = int(capacity)
        except (TypeError, ValueError):
            raise ValidationError("capacity must be an integer")
        if capacity < 0:
            raise ValidationError("capacity must be non-negative")
        return self.repository.set_zone_capacity(zone, capacity)
