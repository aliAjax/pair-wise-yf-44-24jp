import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_ZONE_CAPACITY = 10


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS occupancies (
                    id TEXT PRIMARY KEY,
                    zone TEXT NOT NULL,
                    owner_kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    batch INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancies_owner_active
                    ON occupancies(zone, owner_kind, owner_id)
                    WHERE status IN ('active', 'queued');
                CREATE INDEX IF NOT EXISTS idx_occupancies_zone_status
                    ON occupancies(zone, status);
                CREATE TABLE IF NOT EXISTS zone_capacity (
                    zone TEXT PRIMARY KEY,
                    capacity INTEGER NOT NULL
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True

    # ------------------------------------------------------------------
    # 占用账（occupancy ledger）
    # ------------------------------------------------------------------

    @staticmethod
    def _occupancy_from_row(row):
        return {
            "id": row["id"],
            "zone": row["zone"],
            "owner_kind": row["owner_kind"],
            "owner_id": row["owner_id"],
            "amount": int(row["amount"]),
            "status": row["status"],
            "batch": int(row["batch"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _zone_capacity(self, connection, zone):
        row = connection.execute(
            "SELECT capacity FROM zone_capacity WHERE zone = ?", (zone,)
        ).fetchone()
        return int(row["capacity"]) if row else DEFAULT_ZONE_CAPACITY

    def _zone_occupied(self, connection, zone):
        row = connection.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM occupancies "
            "WHERE zone = ? AND status = 'active'",
            (zone,),
        ).fetchone()
        return int(row["total"])

    def _next_batch(self, connection, zone):
        row = connection.execute(
            "SELECT COALESCE(MAX(batch), 0) AS last FROM occupancies WHERE zone = ?",
            (zone,),
        ).fetchone()
        return int(row["last"]) + 1

    def _get_occupancy(self, connection, zone, owner_kind, owner_id):
        row = connection.execute(
            "SELECT * FROM occupancies WHERE zone = ? AND owner_kind = ? AND owner_id = ? "
            "AND status IN ('active', 'queued') ORDER BY id LIMIT 1",
            (zone, owner_kind, owner_id),
        ).fetchone()
        return row

    def _insert_occupancy(self, connection, zone, owner_kind, owner_id, amount, status):
        now = utcnow()
        connection.execute(
            "INSERT INTO occupancies(id, zone, owner_kind, owner_id, amount, status, batch, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                zone,
                owner_kind,
                owner_id,
                int(amount),
                status,
                self._next_batch(connection, zone),
                now,
                now,
            ),
        )

    def occupy_and_transition(self, entity_id, expected_version, next_status, merged_data,
                              zone, owner_kind, amount, force=False):
        """占用与实体状态更新在同一写事务内完成，保证"占用即状态"。

        变更单实施时若容量不足，实体状态落为 queued（排队），占用也为 queued；
        其余对象按 next_status 更新。返回 (occ_status, updated_entity)。
        """
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            total = self._zone_capacity(connection, zone)
            occupied = self._zone_occupied(connection, zone)
            available = total - occupied
            existing = self._get_occupancy(connection, zone, owner_kind, entity_id)
            if existing:
                occ_status = existing["status"]
            else:
                occ_status = "active" if (force or available >= int(amount)) else "queued"
                self._insert_occupancy(connection, zone, owner_kind, entity_id, amount, occ_status)
            if owner_kind == "change":
                actual_status = "implemented" if occ_status == "active" else "queued"
            else:
                actual_status = next_status
            payload = json.dumps(merged_data, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (actual_status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return occ_status, self.get_entity(entity_id)

    def release_and_transition(self, entity_id, expected_version, next_status, merged_data,
                               owner_kind, invalidate_queued=False):
        """释放对象的有效占用并更新实体状态，随后重排所在管廊的队列。

        invalidate_queued=True 时（安全员冻结），仅使未实施的 queued 占用失效，
        active 占用保持不变。返回更新后的实体。
        """
        now = utcnow()
        connection = self._connect()
        zone = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            occ = connection.execute(
                "SELECT * FROM occupancies WHERE owner_kind = ? AND owner_id = ? "
                "AND status IN ('active', 'queued') ORDER BY id LIMIT 1",
                (owner_kind, entity_id),
            ).fetchone()
            if occ:
                zone = occ["zone"]
                if invalidate_queued:
                    new_status = "invalid" if occ["status"] == "queued" else None
                else:
                    new_status = "released"
                if new_status:
                    connection.execute(
                        "UPDATE occupancies SET status = ?, updated_at = ? WHERE id = ?",
                        (new_status, now, occ["id"]),
                    )
            payload = json.dumps(merged_data, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (next_status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if zone:
            self.rearrange_zone(zone)
        return self.get_entity(entity_id)

    def rearrange_zone(self, zone):
        """按 FIFO 重排管廊队列：容量允许时依次把 queued 占用置为 active，
        并把对应的 queued 变更单自动置为 implemented。返回被激活的占用 id 列表。
        """
        now = utcnow()
        activated = []
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            total = self._zone_capacity(connection, zone)
            occupied = self._zone_occupied(connection, zone)
            available = total - occupied
            queue = connection.execute(
                "SELECT * FROM occupancies WHERE zone = ? AND status = 'queued' ORDER BY id",
                (zone,),
            ).fetchall()
            for occ in queue:
                if available >= int(occ["amount"]):
                    connection.execute(
                        "UPDATE occupancies SET status = 'active', batch = batch + 1, updated_at = ? "
                        "WHERE id = ? AND status = 'queued'",
                        (now, occ["id"]),
                    )
                    available -= int(occ["amount"])
                    activated.append(occ["id"])
                    if occ["owner_kind"] == "change":
                        connection.execute(
                            "UPDATE entities SET status = 'implemented', version = version + 1, "
                            "updated_at = ? WHERE id = ? AND status = 'queued'",
                            (now, occ["owner_id"]),
                        )
                else:
                    break
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return activated

    def list_occupancies(self, zone=None, status=None):
        clauses = []
        params = []
        if zone:
            clauses.append("zone = ?")
            params.append(zone)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM occupancies" + where + " ORDER BY id", params
            ).fetchall()
        return [self._occupancy_from_row(row) for row in rows]

    def get_occupancy(self, occupancy_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM occupancies WHERE id = ?", (occupancy_id,)
            ).fetchone()
        return self._occupancy_from_row(row) if row else None

    def zone_status(self, zone):
        with self._connect() as connection:
            total = self._zone_capacity(connection, zone)
            occupied = self._zone_occupied(connection, zone)
            active_rows = connection.execute(
                "SELECT * FROM occupancies WHERE zone = ? AND status = 'active' ORDER BY id",
                (zone,),
            ).fetchall()
            queue_rows = connection.execute(
                "SELECT * FROM occupancies WHERE zone = ? AND status = 'queued' ORDER BY id",
                (zone,),
            ).fetchall()
        return {
            "zone": zone,
            "capacity": total,
            "occupied": occupied,
            "available": total - occupied,
            "active": [self._occupancy_from_row(row) for row in active_rows],
            "queue": [self._occupancy_from_row(row) for row in queue_rows],
        }

    def list_zones(self):
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT zone FROM (
                    SELECT zone AS zone FROM zone_capacity
                    UNION
                    SELECT zone FROM occupancies
                    UNION
                    SELECT COALESCE(json_extract(data, '$.zone'),
                                    json_extract(data, '$.location')) AS zone
                    FROM entities WHERE kind = 'unit'
                ) WHERE zone IS NOT NULL ORDER BY zone
                """
            ).fetchall()
        return [self.zone_status(row["zone"]) for row in rows]

    def set_zone_capacity(self, zone, capacity):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO zone_capacity(zone, capacity) VALUES (?, ?)",
                (zone, int(capacity)),
            )
        return self.zone_status(zone)
