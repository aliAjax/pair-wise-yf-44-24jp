import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Transaction:
    """One SQLite write transaction. BEGIN IMMEDIATE serialises writers so
    that two concurrent capacity claims for the same corridor cannot both win."""

    def __init__(self, repository):
        self._repository = repository
        self.connection = None

    def __enter__(self):
        self.connection = self._repository._connect()
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
        return False


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def transaction(self):
        return _Transaction(self)

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
                    scope TEXT NOT NULL DEFAULT 'create',
                    entity_id TEXT NOT NULL,
                    result_status TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key, scope)
                );
                CREATE TABLE IF NOT EXISTS occupancy_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    corridor_id TEXT NOT NULL,
                    holder_type TEXT NOT NULL,
                    holder_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    queued_at TEXT,
                    created_at TEXT NOT NULL,
                    released_at TEXT,
                    release_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_occ_corridor_status
                    ON occupancy_ledger(corridor_id, status, id);
                CREATE INDEX IF NOT EXISTS idx_occ_holder
                    ON occupancy_ledger(holder_type, holder_id, status);
            """)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(idempotency)")
            }
            if "scope" not in columns:
                connection.execute(
                    "ALTER TABLE idempotency ADD COLUMN scope TEXT NOT NULL DEFAULT 'create'"
                )

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

    # ----- entities -------------------------------------------------------

    @staticmethod
    def _insert_entity_conn(conn, entity_id, kind, status, data, actor_id, now=None):
        now = now or utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        conn.execute(
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (entity_id, kind, status, payload, actor_id, now, now),
        )

    @staticmethod
    def _get_entity_conn(conn, entity_id):
        row = conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return SQLiteRepository._entity_from_row(row) if row else None

    @staticmethod
    def _list_entities_conn(conn, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
        ).fetchall()
        return [SQLiteRepository._entity_from_row(row) for row in rows]

    @staticmethod
    def _update_entity_conn(conn, entity_id, expected_version, status, data, now=None):
        now = now or utcnow()
        row = conn.execute(
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
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        conn.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (status, payload, now, entity_id, current_version),
        )
        return current_version + 1

    def create_entity(self, entity_id, kind, status, data, actor_id):
        with self.transaction() as conn:
            self._insert_entity_conn(conn, entity_id, kind, status, data, actor_id)
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            return self._get_entity_conn(connection, entity_id)

    def list_entities(self, kind=None, status=None):
        with self._connect() as connection:
            return self._list_entities_conn(connection, kind=kind, status=status)

    def find_entities_conn(self, conn, kind, field, value):
        return [
            entity
            for entity in self._list_entities_conn(conn, kind=kind)
            if (entity["id"] == value
                if field == "id"
                else entity["data"].get(field) == value)
        ]

    def find_entities(self, kind, field, value):
        with self._connect() as connection:
            return self.find_entities_conn(connection, kind, field, value)

    def update_entity(self, entity_id, expected_version, status, data):
        with self.transaction() as conn:
            self._update_entity_conn(conn, entity_id, expected_version, status, data)
        return self.get_entity(entity_id)

    # ----- audit ----------------------------------------------------------

    @staticmethod
    def _append_audit_conn(conn, entity_id, actor_id, actor_role, action,
                           from_status, to_status, detail, now=None):
        conn.execute(
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
                now or utcnow(),
            ),
        )

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status,
                     to_status, detail):
        with self._connect() as connection:
            self._append_audit_conn(
                connection, entity_id, actor_id, actor_role, action,
                from_status, to_status, detail,
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id",
                    (entity_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM audit_log ORDER BY id"
                ).fetchall()
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

    # ----- idempotency ----------------------------------------------------

    @staticmethod
    def _get_idempotency_conn(conn, actor_id, idem_key, scope):
        row = conn.execute(
            "SELECT entity_id, result_status FROM idempotency "
            "WHERE actor_id = ? AND idem_key = ? AND scope = ?",
            (actor_id, idem_key, scope),
        ).fetchone()
        if not row:
            return None
        return {"entity_id": row["entity_id"], "result_status": row["result_status"]}

    @staticmethod
    def _save_idempotency_conn(conn, actor_id, idem_key, scope, entity_id,
                               result_status, now=None):
        conn.execute(
            "INSERT INTO idempotency(actor_id, idem_key, scope, entity_id, result_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (actor_id, idem_key, scope, entity_id, result_status, now or utcnow()),
        )

    def get_idempotency(self, actor_id, idem_key, scope="create"):
        with self._connect() as connection:
            row = self._get_idempotency_conn(connection, actor_id, idem_key, scope)
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, scope, entity_id, created_at) "
                "VALUES (?, ?, 'create', ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # ----- occupancy ledger ----------------------------------------------

    @staticmethod
    def _insert_occupancy_conn(conn, corridor_id, holder_type, holder_id, amount,
                               reason, status, queued_at=None, now=None):
        now = now or utcnow()
        cur = conn.execute(
            "INSERT INTO occupancy_ledger(corridor_id, holder_type, holder_id, amount, "
            "reason, status, queued_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (corridor_id, holder_type, holder_id, amount, reason, status,
             queued_at, now),
        )
        return cur.lastrowid

    @staticmethod
    def _release_occupancy_conn(conn, holder_type, holder_id, release_reason,
                                corridor_id=None, now=None):
        """Void every active row held by one holder: the old occupation is
        invalidated and recomputed by the caller inserting a fresh row."""
        now = now or utcnow()
        sql = (
            "UPDATE occupancy_ledger SET status = 'released', released_at = ?, "
            "release_reason = ? WHERE holder_type = ? AND holder_id = ? "
            "AND status = 'active'"
        )
        params = [now, release_reason, holder_type, holder_id]
        if corridor_id is not None:
            sql += " AND corridor_id = ?"
            params.append(corridor_id)
        cur = conn.execute(sql, params)
        return cur.rowcount

    @staticmethod
    def _cancel_waiting_conn(conn, holder_type, holder_id, release_reason,
                             corridor_id=None, now=None):
        """Void queued place-holders (e.g. a safety freeze voids not-yet-implemented
        occupations, or a queued change is promoted into implementation)."""
        now = now or utcnow()
        sql = (
            "UPDATE occupancy_ledger SET status = 'cancelled', released_at = ?, "
            "release_reason = ? WHERE holder_type = ? AND holder_id = ? "
            "AND status = 'waiting'"
        )
        params = [now, release_reason, holder_type, holder_id]
        if corridor_id is not None:
            sql += " AND corridor_id = ?"
            params.append(corridor_id)
        cur = conn.execute(sql, params)
        return cur.rowcount

    @staticmethod
    def _active_usage_conn(conn, corridor_id):
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS used FROM occupancy_ledger "
            "WHERE corridor_id = ? AND status = 'active'",
            (corridor_id,),
        ).fetchone()
        return int(row["used"])

    @staticmethod
    def _has_waiting_conn(conn, corridor_id):
        row = conn.execute(
            "SELECT 1 FROM occupancy_ledger WHERE corridor_id = ? "
            "AND status = 'waiting' LIMIT 1",
            (corridor_id,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _list_occupancy_conn(conn, corridor_id, status=None):
        sql = (
            "SELECT * FROM occupancy_ledger WHERE corridor_id = ? "
            "ORDER BY id"
        )
        params = [corridor_id]
        if status:
            sql = "SELECT * FROM occupancy_ledger WHERE corridor_id = ? AND status = ? ORDER BY id"
            params.append(status)
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def active_usage(self, corridor_id):
        with self._connect() as connection:
            return self._active_usage_conn(connection, corridor_id)

    def list_occupancy(self, corridor_id):
        with self._connect() as connection:
            return self._list_occupancy_conn(connection, corridor_id)

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
