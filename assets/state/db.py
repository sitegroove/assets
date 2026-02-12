"""Database manager — SQLite connection handling and schema management."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
COMPILED_SCHEMA_VERSION = 1

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
    name         TEXT    NOT NULL,
    kind         TEXT    NOT NULL DEFAULT '',
    fingerprint  TEXT    NOT NULL,
    data         TEXT    NOT NULL DEFAULT '{}',
    source_files TEXT    NOT NULL DEFAULT '[]',
    applied_at   TEXT    NOT NULL,
    applied_by   TEXT    NOT NULL DEFAULT '',
    version      INTEGER NOT NULL DEFAULT 1,
    deleted      INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (environment, name),
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
    name         TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    kind         TEXT    NOT NULL DEFAULT '',
    fingerprint  TEXT    NOT NULL,
    data         TEXT    NOT NULL DEFAULT '{}',
    source_files TEXT    NOT NULL DEFAULT '[]',
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
CREATE INDEX IF NOT EXISTS idx_assets_kind        ON assets(environment, kind);
CREATE INDEX IF NOT EXISTS idx_assets_fingerprint ON assets(environment, fingerprint);
CREATE INDEX IF NOT EXISTS idx_deps_env           ON dependencies(environment);
CREATE INDEX IF NOT EXISTS idx_deps_target        ON dependencies(environment, target);
CREATE INDEX IF NOT EXISTS idx_history_asset      ON assets_history(environment, name, version);
CREATE INDEX IF NOT EXISTS idx_history_time       ON assets_history(environment, recorded_at);
CREATE INDEX IF NOT EXISTS idx_history_author     ON assets_history(applied_by);

-- ─── Triggers for automatic history ────────────────────────
CREATE TRIGGER IF NOT EXISTS trg_assets_insert AFTER INSERT ON assets
BEGIN
    INSERT INTO assets_history
        (environment, name, action, kind, fingerprint, data,
         source_files, applied_at, applied_by, version)
    VALUES
        (NEW.environment, NEW.name, 'create', NEW.kind, NEW.fingerprint,
         NEW.data, NEW.source_files, NEW.applied_at, NEW.applied_by, NEW.version);
END;

CREATE TRIGGER IF NOT EXISTS trg_assets_update AFTER UPDATE ON assets
BEGIN
    INSERT INTO assets_history
        (environment, name, action, kind, fingerprint, data,
         source_files, applied_at, applied_by, version)
    VALUES
        (NEW.environment, NEW.name, 'update', NEW.kind, NEW.fingerprint,
         NEW.data, NEW.source_files, NEW.applied_at, NEW.applied_by, NEW.version);
END;

CREATE TRIGGER IF NOT EXISTS trg_assets_delete AFTER DELETE ON assets
BEGIN
    INSERT INTO assets_history
        (environment, name, action, kind, fingerprint, data,
         source_files, applied_at, applied_by, version)
    VALUES
        (OLD.environment, OLD.name, 'delete', OLD.kind, OLD.fingerprint,
         OLD.data, OLD.source_files, OLD.applied_at, OLD.applied_by, OLD.version);
END;

-- ─── Schema version tracking ──────────────────────────────
CREATE TABLE IF NOT EXISTS schema_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 1);
"""

COMPILED_SCHEMA = """\
CREATE TABLE IF NOT EXISTS compiled (
    path         TEXT PRIMARY KEY,
    source_mtime INTEGER NOT NULL,
    content_hash TEXT    NOT NULL,
    data         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 1);
"""


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply performance and safety pragmas."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def connect_state(db_path: str | Path) -> sqlite3.Connection:
    """Open (or create) the state database, applying schema if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _configure_connection(conn)
    conn.executescript(STATE_SCHEMA)
    logger.debug("State database ready at %s", db_path)
    return conn


def connect_compiled(db_path: str | Path) -> sqlite3.Connection:
    """Open (or create) the compiled-cache database, applying schema if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _configure_connection(conn)
    conn.executescript(COMPILED_SCHEMA)
    logger.debug("Compiled cache database ready at %s", db_path)
    return conn
