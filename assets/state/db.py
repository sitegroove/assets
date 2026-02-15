"""Database manager — SQLite connection handling and schema bootstrap.

Two separate databases:

- **state.db** (per-environment directory): asset state, dependencies,
  and append-only history.  One database per environment, synced to
  remote storage when using ``TieredBackend``.

- **index.db** (local-only, at the base path): file-index entries and
  their dependency edges.  Never synced to remote.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── State schema (one DB per environment) ─────────────────────────

STATE_SCHEMA = """\
-- ─── State metadata ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS state_metadata (
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}'
);

-- ─── Asset state ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS assets (
    id           TEXT    PRIMARY KEY,
    type         TEXT    NOT NULL DEFAULT '',
    fingerprint  TEXT    NOT NULL,
    data         TEXT    NOT NULL DEFAULT '{}',
    applied_at   TEXT    NOT NULL,
    applied_by   TEXT    NOT NULL DEFAULT '',
    version      INTEGER NOT NULL DEFAULT 1
);

-- ─── Dependencies ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dependencies (
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',

    PRIMARY KEY (source, target, type)
);

-- ─── Asset history (append-only) ───────────────────────────
CREATE TABLE IF NOT EXISTS assets_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id     TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    type         TEXT    NOT NULL DEFAULT '',
    fingerprint  TEXT    NOT NULL,
    data         TEXT    NOT NULL DEFAULT '{}',
    applied_at   TEXT    NOT NULL,
    applied_by   TEXT    NOT NULL DEFAULT '',
    version      INTEGER NOT NULL,
    recorded_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ─── Dependency history (append-only) ──────────────────────
CREATE TABLE IF NOT EXISTS dependencies_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    action       TEXT NOT NULL,
    source       TEXT NOT NULL,
    target       TEXT NOT NULL,
    type         TEXT NOT NULL DEFAULT '',
    fingerprint  TEXT NOT NULL,
    data         TEXT NOT NULL DEFAULT '{}',
    recorded_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ─── Indexes ───────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_assets_type        ON assets(type);
CREATE INDEX IF NOT EXISTS idx_assets_fingerprint ON assets(fingerprint);
CREATE INDEX IF NOT EXISTS idx_deps_target        ON dependencies(target);
CREATE INDEX IF NOT EXISTS idx_history_asset
    ON assets_history(asset_id, version);
CREATE INDEX IF NOT EXISTS idx_history_time
    ON assets_history(recorded_at);
CREATE INDEX IF NOT EXISTS idx_history_author     ON assets_history(applied_by);

-- ─── Triggers for automatic history ────────────────────────
DROP TRIGGER IF EXISTS trg_assets_insert;
DROP TRIGGER IF EXISTS trg_assets_update;
DROP TRIGGER IF EXISTS trg_assets_delete;
DROP TRIGGER IF EXISTS trg_deps_insert;
DROP TRIGGER IF EXISTS trg_deps_update;
DROP TRIGGER IF EXISTS trg_deps_delete;

CREATE TRIGGER IF NOT EXISTS trg_assets_insert AFTER INSERT ON assets
BEGIN
    INSERT INTO assets_history
        (asset_id, action, type, fingerprint, data,
         applied_at, applied_by, version)
    VALUES
        (NEW.id, 'create', NEW.type, NEW.fingerprint,
         NEW.data, NEW.applied_at, NEW.applied_by, NEW.version);
END;

CREATE TRIGGER IF NOT EXISTS trg_assets_update AFTER UPDATE ON assets
    WHEN OLD.fingerprint != NEW.fingerprint
BEGIN
    INSERT INTO assets_history
        (asset_id, action, type, fingerprint, data,
         applied_at, applied_by, version)
    VALUES
        (NEW.id, 'update', NEW.type, NEW.fingerprint,
         NEW.data, NEW.applied_at, NEW.applied_by, NEW.version);
END;

CREATE TRIGGER IF NOT EXISTS trg_assets_delete AFTER DELETE ON assets
BEGIN
    INSERT INTO assets_history
        (asset_id, action, type, fingerprint, data,
         applied_at, applied_by, version)
    VALUES
        (OLD.id, 'delete', OLD.type, OLD.fingerprint,
         OLD.data, OLD.applied_at, OLD.applied_by, OLD.version);
END;

-- ─── Triggers for automatic dependency history ─────────────
CREATE TRIGGER IF NOT EXISTS trg_deps_insert AFTER INSERT ON dependencies
BEGIN
    INSERT INTO dependencies_history
        (action, source, target, type, fingerprint, data)
    VALUES
        ('create', NEW.source, NEW.target, NEW.type,
         NEW.fingerprint, NEW.data);
END;

CREATE TRIGGER IF NOT EXISTS trg_deps_update AFTER UPDATE ON dependencies
    WHEN OLD.fingerprint != NEW.fingerprint
       OR OLD.source != NEW.source
       OR OLD.target != NEW.target
BEGIN
    INSERT INTO dependencies_history
        (action, source, target, type, fingerprint, data)
    VALUES
        ('update', NEW.source, NEW.target, NEW.type,
         NEW.fingerprint, NEW.data);
END;

CREATE TRIGGER IF NOT EXISTS trg_deps_delete AFTER DELETE ON dependencies
BEGIN
    INSERT INTO dependencies_history
        (action, source, target, type, fingerprint, data)
    VALUES
        ('delete', OLD.source, OLD.target, OLD.type,
         OLD.fingerprint, OLD.data);
END;
"""

# ─── Index schema (local-only, separate DB) ────────────────────────

INDEX_SCHEMA = """\
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
"""


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply performance and safety pragmas."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def connect_state(db_path: str | Path) -> sqlite3.Connection:
    """Open (or create) a per-environment state database.

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
    conn.executescript(STATE_SCHEMA)
    logger.debug("State database ready at %s", str_path)
    return conn


def connect_index(db_path: str | Path) -> sqlite3.Connection:
    """Open (or create) the local-only file index database.

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
    conn.executescript(INDEX_SCHEMA)
    logger.debug("Index database ready at %s", str_path)
    return conn
