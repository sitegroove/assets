"""Database manager — SQLite connection handling and schema management."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 3
STATE_SCHEMA = """\
-- ─── Environment metadata ──────────────────────────────────
CREATE TABLE IF NOT EXISTS environments (
    name        TEXT PRIMARY KEY,
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}'
);

-- ─── Asset state ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS assets (
    environment  TEXT    NOT NULL,
    id           TEXT    NOT NULL,
    type         TEXT    NOT NULL DEFAULT '',
    fingerprint  TEXT    NOT NULL,
    data         TEXT    NOT NULL DEFAULT '{}',
    applied_at   TEXT    NOT NULL,
    applied_by   TEXT    NOT NULL DEFAULT '',
    version      INTEGER NOT NULL DEFAULT 1,
    deleted      INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (environment, id),
    FOREIGN KEY (environment) REFERENCES environments(name)
);

-- ─── Dependencies ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dependencies (
    environment TEXT NOT NULL,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',

    PRIMARY KEY (environment, source, target, type),
    FOREIGN KEY (environment) REFERENCES environments(name)
);

-- ─── Asset history (append-only) ───────────────────────────
CREATE TABLE IF NOT EXISTS assets_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    environment  TEXT    NOT NULL,
    asset_id     TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    type         TEXT    NOT NULL DEFAULT '',
    fingerprint  TEXT    NOT NULL,
    data         TEXT    NOT NULL DEFAULT '{}',
    applied_at   TEXT    NOT NULL,
    applied_by   TEXT    NOT NULL DEFAULT '',
    version      INTEGER NOT NULL,
    recorded_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    FOREIGN KEY (environment) REFERENCES environments(name)
);

-- ─── Dependency history (append-only) ──────────────────────
CREATE TABLE IF NOT EXISTS dependencies_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    environment  TEXT NOT NULL,
    action       TEXT NOT NULL,
    source       TEXT NOT NULL,
    target       TEXT NOT NULL,
    type         TEXT NOT NULL DEFAULT '',
    fingerprint  TEXT NOT NULL,
    data         TEXT NOT NULL DEFAULT '{}',
    recorded_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    FOREIGN KEY (environment) REFERENCES environments(name)
);

-- ─── Indexes ───────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_assets_env         ON assets(environment);
CREATE INDEX IF NOT EXISTS idx_assets_type        ON assets(environment, type);
CREATE INDEX IF NOT EXISTS idx_assets_fingerprint ON assets(environment, fingerprint);
CREATE INDEX IF NOT EXISTS idx_deps_env           ON dependencies(environment);
CREATE INDEX IF NOT EXISTS idx_deps_target        ON dependencies(environment, target);
CREATE INDEX IF NOT EXISTS idx_history_asset
    ON assets_history(environment, asset_id, version);
CREATE INDEX IF NOT EXISTS idx_history_time
    ON assets_history(environment, recorded_at);
CREATE INDEX IF NOT EXISTS idx_history_author     ON assets_history(applied_by);

-- ─── Triggers for automatic history ────────────────────────
-- Drop old triggers without WHEN guards (schema v2 → v3 migration)
DROP TRIGGER IF EXISTS trg_assets_insert;
DROP TRIGGER IF EXISTS trg_assets_update;
DROP TRIGGER IF EXISTS trg_assets_delete;
DROP TRIGGER IF EXISTS trg_deps_insert;
DROP TRIGGER IF EXISTS trg_deps_update;
DROP TRIGGER IF EXISTS trg_deps_delete;

CREATE TRIGGER IF NOT EXISTS trg_assets_insert AFTER INSERT ON assets
BEGIN
    INSERT INTO assets_history
        (environment, asset_id, action, type, fingerprint, data,
         applied_at, applied_by, version)
    VALUES
        (NEW.environment, NEW.id, 'create', NEW.type, NEW.fingerprint,
         NEW.data, NEW.applied_at, NEW.applied_by, NEW.version);
END;

CREATE TRIGGER IF NOT EXISTS trg_assets_update AFTER UPDATE ON assets
    WHEN OLD.fingerprint != NEW.fingerprint
BEGIN
    INSERT INTO assets_history
        (environment, asset_id, action, type, fingerprint, data,
         applied_at, applied_by, version)
    VALUES
        (NEW.environment, NEW.id, 'update', NEW.type, NEW.fingerprint,
         NEW.data, NEW.applied_at, NEW.applied_by, NEW.version);
END;

CREATE TRIGGER IF NOT EXISTS trg_assets_delete AFTER DELETE ON assets
BEGIN
    INSERT INTO assets_history
        (environment, asset_id, action, type, fingerprint, data,
         applied_at, applied_by, version)
    VALUES
        (OLD.environment, OLD.id, 'delete', OLD.type, OLD.fingerprint,
         OLD.data, OLD.applied_at, OLD.applied_by, OLD.version);
END;

-- ─── Triggers for automatic dependency history ─────────────
CREATE TRIGGER IF NOT EXISTS trg_deps_insert AFTER INSERT ON dependencies
BEGIN
    INSERT INTO dependencies_history
        (environment, action, source, target, type, fingerprint, data)
    VALUES
        (NEW.environment, 'create', NEW.source, NEW.target, NEW.type,
         NEW.fingerprint, NEW.data);
END;

CREATE TRIGGER IF NOT EXISTS trg_deps_update AFTER UPDATE ON dependencies
    WHEN OLD.fingerprint != NEW.fingerprint
       OR OLD.source != NEW.source
       OR OLD.target != NEW.target
BEGIN
    INSERT INTO dependencies_history
        (environment, action, source, target, type, fingerprint, data)
    VALUES
        (NEW.environment, 'update', NEW.source, NEW.target, NEW.type,
         NEW.fingerprint, NEW.data);
END;

CREATE TRIGGER IF NOT EXISTS trg_deps_delete AFTER DELETE ON dependencies
BEGIN
    INSERT INTO dependencies_history
        (environment, action, source, target, type, fingerprint, data)
    VALUES
        (OLD.environment, 'delete', OLD.source, OLD.target, OLD.type,
         OLD.fingerprint, OLD.data);
END;

-- ─── File index entries ────────────────────────────────────
CREATE TABLE IF NOT EXISTS index_entries (
    grp          TEXT NOT NULL DEFAULT '',
    location     TEXT NOT NULL,
    asset_id     TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    content_hash TEXT NOT NULL,

    PRIMARY KEY (grp, location)
);

-- ─── File index entry dependencies ────────────────────────
CREATE TABLE IF NOT EXISTS index_deps (
    entry_grp      TEXT NOT NULL DEFAULT '',
    entry_location TEXT NOT NULL,
    dep_grp        TEXT NOT NULL DEFAULT '',
    dep_location   TEXT NOT NULL,
    dep_kind       TEXT NOT NULL DEFAULT '',
    dep_hash       TEXT NOT NULL,

    PRIMARY KEY (entry_grp, entry_location, dep_grp, dep_location),
    FOREIGN KEY (entry_grp, entry_location)
        REFERENCES index_entries(grp, location) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_index_deps_dep
    ON index_deps(dep_grp, dep_location);

-- ─── Schema version tracking ──────────────────────────────
CREATE TABLE IF NOT EXISTS schema_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 3);
"""


def _maybe_migrate(conn: sqlite3.Connection) -> None:
    """Run forward migrations when the stored version is behind.

    Each migration step applies DDL changes and bumps the stored
    version.  Migrations are idempotent (``IF NOT EXISTS`` /
    ``DROP ... IF EXISTS``) so re-running is safe.
    """
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        # Table doesn't exist yet — schema will be created fresh
        return

    if row is None:
        return

    stored = row[0] if isinstance(row, (tuple, list)) else row["version"]

    if stored < 3:
        # v2 → v3: add WHEN guards to history triggers
        conn.executescript(
            """\
            DROP TRIGGER IF EXISTS trg_assets_insert;
            DROP TRIGGER IF EXISTS trg_assets_update;
            DROP TRIGGER IF EXISTS trg_assets_delete;
            DROP TRIGGER IF EXISTS trg_deps_insert;
            DROP TRIGGER IF EXISTS trg_deps_update;
            DROP TRIGGER IF EXISTS trg_deps_delete;
            """
        )
        conn.execute("UPDATE schema_version SET version = 3 WHERE id = 1")
        conn.commit()
        logger.info("Migrated state schema from v%d to v3", stored)


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply performance and safety pragmas."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def connect_state(db_path: str | Path) -> sqlite3.Connection:
    """Open (or create) the state database, applying schema if needed.

    Accepts ``":memory:"`` for a transient in-memory database (useful
    for testing).  In that case no filesystem directories are created.
    """
    str_path = str(db_path)
    if str_path != ":memory:":
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str_path)
    conn.row_factory = sqlite3.Row
    _configure_connection(conn)
    _maybe_migrate(conn)
    conn.executescript(STATE_SCHEMA)
    logger.debug("State database ready at %s", str_path)
    return conn
