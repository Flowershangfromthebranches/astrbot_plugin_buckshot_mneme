"""Buckshot 内聚记忆存储：SQLite 单文件 + FTS5。

从 mneme-play 的 memory/db.py 抽取，去掉与 AstrBot 无关的服务化表，
保留 FTS5、派生向量表的扩展位，并增加对局/玩家/人格/记忆层次字段。
"""
from __future__ import annotations

import sqlite3

SCHEMA_V1 = """
CREATE TABLE memories (
  id            TEXT PRIMARY KEY,
  layer         TEXT NOT NULL CHECK (layer IN ('profile','heuristic')),
  player_id     TEXT,
  persona_id    TEXT,
  text          TEXT NOT NULL,
  tags          TEXT NOT NULL DEFAULT '[]',
  confidence    TEXT NOT NULL DEFAULT 'speculated'
                CHECK (confidence IN ('verified','speculated','disputed')),
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','superseded','retracted','disputed','archived')),
  evidence      TEXT,
  match_id      TEXT,
  importance    INTEGER NOT NULL DEFAULT 2 CHECK (importance BETWEEN 1 AND 3),
  observed_at   TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  supersedes_id TEXT REFERENCES memories (id) ON DELETE SET NULL
);
CREATE INDEX memories_scope ON memories (layer, player_id, persona_id, status);

CREATE VIRTUAL TABLE memory_fts USING fts5 (
  memory_id UNINDEXED, tokens, tokenize = 'unicode61'
);

CREATE TABLE tactical_notes (
  id          TEXT PRIMARY KEY,
  match_id    TEXT NOT NULL,
  player_id   TEXT,
  kind        TEXT NOT NULL CHECK (kind IN ('shell','observation','goal','private')),
  text        TEXT NOT NULL,
  tags        TEXT NOT NULL DEFAULT '[]',
  confidence  TEXT NOT NULL DEFAULT 'verified'
              CHECK (confidence IN ('verified','speculated','disputed')),
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','archived')),
  created_at  TEXT NOT NULL,
  expires_at  TEXT
);
CREATE INDEX tactical_scope ON tactical_notes (match_id, status, kind);

CREATE VIRTUAL TABLE tactical_fts USING fts5 (
  note_id UNINDEXED, tokens, tokenize = 'unicode61'
);

CREATE TABLE matches (
  id          TEXT PRIMARY KEY,
  player_id   TEXT NOT NULL,
  persona_id  TEXT,
  memory_mode TEXT NOT NULL DEFAULT 'adaptive',
  started_at  TEXT NOT NULL,
  ended_at    TEXT,
  result      TEXT CHECK (result IN ('win','loss','draw','aborted')),
  score       TEXT,
  meta        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX matches_player ON matches (player_id, started_at);

CREATE TABLE policies (
  player_id    TEXT NOT NULL,
  persona_key  TEXT NOT NULL DEFAULT '*',
  mode         TEXT NOT NULL DEFAULT 'auto'
               CHECK (mode IN ('auto','off','on','adaptive')),
  learn        INTEGER NOT NULL DEFAULT 1,
  reason       TEXT,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (player_id, persona_key)
);

-- 可选向量表：保留 mneme 的 BLOB 设计，未配置 embedder 时不写。
CREATE TABLE embeddings (
  memory_id TEXT PRIMARY KEY REFERENCES memories (id) ON DELETE CASCADE,
  model     TEXT NOT NULL,
  dim       INTEGER NOT NULL,
  vec       BLOB NOT NULL
);

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

MIGRATIONS: list[str] = [SCHEMA_V1]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA secure_delete = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    try:
        migrate(conn)
    except sqlite3.OperationalError as exc:
        if "fts5" in str(exc).lower():
            raise RuntimeError("当前 SQLite 缺少 FTS5，Buckshot 记忆检索无法工作") from exc
        raise
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(MIGRATIONS):
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise RuntimeError(f"迁移后外键校验失败：{[tuple(r) for r in broken[:5]]}")
