from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# Capacity weight of a change, keyed by its risk level.
RISK_OCCUPANCY = {"low": 1, "medium": 2, "high": 3, "critical": 4}
# Action items reserve one slot of public corridor capacity while open.
ACTION_ITEM_OCCUPANCY = 1
# Default occupation of a shutdown/frozen unit (can be overridden per unit).
DEFAULT_UNIT_OCCUPANCY = 1


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def risk_occupancy(risk_level):
    return RISK_OCCUPANCY.get(str(risk_level).lower(), 4)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _validate_corridor(actor, data, lookup):
    if not data.get("name", "").strip():
        raise ValidationError("corridor name is required")
    try:
        capacity = int(data.get("capacity"))
    except (TypeError, ValueError):
        raise ValidationError("corridor capacity must be a non-negative integer")
    if capacity < 0:
        raise ValidationError("corridor capacity must be a non-negative integer")


def _validate_unit(actor, data, lookup):
    if not data.get("name", "").strip():
        raise ValidationError("unit name is required")
    if not data.get("location", "").strip():
        raise ValidationError("unit location is required")
    corridor_id = data.get("corridor_id")
    if corridor_id is not None and not _find_one(lookup, "corridor", "id", corridor_id):
        raise ValidationError("corridor does not exist")
    load = data.get("unit_occupancy", DEFAULT_UNIT_OCCUPANCY)
    try:
        if int(load) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValidationError("unit_occupancy must be a positive integer")


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def _validate_action_item(actor, data, lookup):
    change = _find_one(lookup, "change", "id", data.get("change_id"))
    if not change:
        raise ValidationError("change does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("action item description is required")


def _validate_assess(actor, entity, data, lookup):
    level = str(data.get("risk_level", "")).lower()
    if level not in RISK_OCCUPANCY:
        raise ValidationError("risk_level must be one of low, medium, high, critical")
    return {
        "required_approvals": required_approval_level(level),
        "risk_level": level,
    }


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    return {"approved_by": actor.user_id}


def _validate_implement(actor, entity, data, lookup):
    if "risk_level" not in entity["data"]:
        raise ValidationError("change has not been risk assessed")
    return {}


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    return {"commissioned_by": actor.user_id}


CUSTOM_CREATE = {
    "corridor": _validate_corridor,
    "unit": _validate_unit,
    "change": _validate_change,
    "action_item": _validate_action_item,
}
CUSTOM_TRANSITIONS = {
    ("change", "assess"): _validate_assess,
    ("change", "approve"): _validate_approve,
    ("change", "implement"): _validate_implement,
    ("change", "commission"): _validate_commission,
}


class RuleEngine:
    ALIASES = {
        "units": "unit",
        "changes": "change",
        "action_items": "action_item",
        "corridors": "corridor",
    }
    INITIAL_STATUS = {
        "corridor": "active",
        "unit": "operating",
        "change": "draft",
        "action_item": "open",
    }
    # 'implement' may land in 'queued' instead of 'implemented'; the service
    # decides the real next status after checking remaining corridor capacity.
    QUEUEABLE_STATUS = "queued"
    # Internal promotion: queued -> implemented when capacity frees up.
    PROMOTE_ACTION = "promote"

    TRANSITIONS = {
        "unit": {
            "shutdown": (("operating",), "shutdown"),
            "startup": (("shutdown",), "operating"),
            "freeze": (("operating",), "frozen"),
            "unfreeze": (("frozen",), "operating"),
        },
        "change": {
            "assess": (("draft",), "assessed"),
            "approve": (("assessed",), "approved"),
            "implement": (("approved",), "implemented"),
            "commission": (("implemented",), "commissioned"),
            "rollback": (("implemented", "commissioned"), "rolled_back"),
            "close": (("rolled_back",), "closed"),
        },
        "action_item": {
            "complete": (("open",), "completed"),
            "verify": (("completed",), "verified"),
            "reopen": (("verified",), "open"),
        },
    }
    CREATE_REQUIRED = {
        "corridor": ("name", "capacity"),
        "unit": ("name", "location"),
        "change": ("unit_id", "description"),
        "action_item": ("change_id", "description", "owner"),
    }
    ACTION_REQUIRED = {
        ("unit", "shutdown"): ("reason",),
        ("unit", "freeze"): ("reason",),
        ("change", "assess"): ("risk_level", "analyst"),
        ("change", "approve"): ("approvals", "permit_id"),
        ("change", "implement"): ("procedure_version",),
        ("change", "commission"): ("tests_passed",),
        ("change", "rollback"): ("reason",),
        ("change", "close"): ("outcome",),
        ("action_item", "complete"): ("completed_by", "evidence"),
        ("action_item", "verify"): ("verifier",),
        ("action_item", "reopen"): ("reason",),
    }
    CREATE_ROLES = {
        "corridor": ("admin", "engineer"),
        "unit": ("admin", "engineer"),
        "change": ("admin", "engineer"),
        "action_item": ("admin", "safety", "engineer"),
    }
    # A safety officer freeze is a safety action. Operator/admin freezes stay
    # operational; only the safety role voids not-yet-implemented occupations.
    ROLE_ACTIONS = {
        "shutdown": ("admin", "operator"),
        "startup": ("admin", "operator"),
        "freeze": ("admin", "operator", "safety"),
        "unfreeze": ("admin", "operator", "safety"),
        "assess": ("admin", "engineer"),
        "approve": ("admin", "safety"),
        "implement": ("admin", "engineer"),
        "commission": ("admin", "engineer"),
        "rollback": ("admin", "engineer"),
        "close": ("admin", "safety"),
        "complete": ("admin", "engineer", "safety"),
        "verify": ("admin", "verifier", "safety"),
        "reopen": ("admin", "verifier", "safety"),
        "promote": ("*",),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    # ----- occupancy calculations ----------------------------------------

    @staticmethod
    def fits(capacity, used, amount):
        """Pre-implementation margin check."""
        return used + amount <= capacity
