"""Buckshot 记忆召回与单行自然语言渲染。

保留 mneme 的原则：FTS5 先召回，再按层次/玩家过滤，最后按预算截断。
模型的上下文里只看到自然语言行，看不到 SQL 行。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .util import query_tokens, unigram_tokens


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_to_item(row) -> dict:
    return {
        "id": row["id"],
        "layer": row["layer"],
        "player_id": row["player_id"],
        "persona_id": row["persona_id"],
        "text": row["text"],
        "tags": json.loads(row["tags"] or "[]"),
        "confidence": row["confidence"],
        "status": row["status"],
        "evidence": row["evidence"],
        "match_id": row["match_id"],
        "importance": row["importance"],
        "observed_at": row["observed_at"],
    }


def _scope_sql(player_id: str | None, layer: str, persona_id: str | None = None) -> tuple[str, list]:
    """画像按玩家隔离；博弈法则是 AI 自己的跨局技能，默认可跨人格共享。"""
    if layer == "profile":
        if not player_id:
            return "0", []
        return "(m.player_id = ? OR m.player_id IS NULL)", [player_id]
    clauses = ["m.layer = 'heuristic'"]
    args: list = []
    if persona_id:
        clauses.append("(m.persona_id IS NULL OR m.persona_id = ? OR m.persona_id = '*')")
        args.append(persona_id)
    return "(" + " AND ".join(clauses) + ")", args


def search_memories(conn, player_id: str | None, persona_id: str | None, query: str,
                    *, layers=("profile", "heuristic"), limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    tokens = query_tokens(query)
    hits = []
    if tokens:
        match = " OR ".join(f'"{t}"' for t in tokens)
        layer_marks = ",".join("?" * len(layers))
        where = [f"m.layer IN ({layer_marks})", "m.status IN ('active','disputed')"]
        args: list = list(layers)
        scope, scope_args = _scope_sql(player_id, "profile", persona_id)
        if "profile" in layers and "heuristic" in layers:
            where.append("(m.layer = 'heuristic' OR " + scope + ")")
            args += scope_args
        elif "profile" in layers:
            where.append(scope)
            args += scope_args
        hits = conn.execute(
            f"""SELECT m.*, bm25(memory_fts) AS score
                FROM memory_fts f JOIN memories m ON m.id = f.memory_id
                WHERE memory_fts MATCH ? AND {' AND '.join(where)}
                ORDER BY score LIMIT ?""",
            (match, *args, limit * 4),
        ).fetchall()
    if not hits and query:
        uni = unigram_tokens(query)
        if uni:
            match = " OR ".join(f'"{t}"' for t in uni)
            layer_marks = ",".join("?" * len(layers))
            where = [f"m.layer IN ({layer_marks})", "m.status IN ('active','disputed')"]
            args = list(layers)
            scope, scope_args = _scope_sql(player_id, "profile", persona_id)
            if "profile" in layers and "heuristic" in layers:
                where.append("(m.layer = 'heuristic' OR " + scope + ")")
                args += scope_args
            elif "profile" in layers:
                where.append(scope)
                args += scope_args
            hits = conn.execute(
                f"""SELECT m.*, bm25(memory_fts) AS score
                    FROM memory_fts f JOIN memories m ON m.id = f.memory_id
                    WHERE memory_fts MATCH ? AND {' AND '.join(where)}
                    ORDER BY score LIMIT ?""",
                (match, *args, limit * 4),
            ).fetchall()
    items = [row_to_item(row) for row in hits]
    return _rank(items, query, limit)


def top_memories(conn, player_id: str | None, persona_id: str | None, layer: str,
                 limit: int = 4) -> list[dict]:
    scope, args = _scope_sql(player_id, layer, persona_id)
    rows = conn.execute(
        f"""SELECT m.* FROM memories m
            WHERE m.layer = ? AND m.status IN ('active','disputed') AND {scope}
            ORDER BY (m.confidence = 'verified') DESC, m.importance DESC,
                     m.observed_at DESC LIMIT ?""",
        (layer, *args, limit),
    ).fetchall()
    return [row_to_item(row) for row in rows]


def relevant(conn, player_id: str | None, persona_id: str | None, query: str, *,
             team: bool = True, limit: int = 6) -> dict[str, list[dict]]:
    layers = ("profile", "heuristic") if team else ("heuristic",)
    found = search_memories(conn, player_id, persona_id, query, layers=layers, limit=limit)
    result = {"profile": [], "heuristic": []}
    for item in found:
        result.setdefault(item["layer"], []).append(item)
    for layer in layers:
        if not result[layer]:
            result[layer] = top_memories(conn, player_id, persona_id, layer, limit=min(2, limit))
    return result


def _rank(items: list[dict], query: str, limit: int) -> list[dict]:
    tokens = set(query_tokens(query))
    seen = set()
    unique = []
    for item in items:
        key = (item["layer"], item["text"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda item: (
        item["confidence"] == "verified",
        len(tokens & set(query_tokens(item["text"]))),
        item["importance"],
        item["observed_at"],
    ), reverse=True)
    return unique[:limit]


def profile_for(conn, player_id: str, persona_id: str | None, *, limit: int = 6) -> list[dict]:
    return top_memories(conn, player_id, persona_id, "profile", limit=limit)


def heuristics_for(conn, player_id: str | None, persona_id: str | None, *, limit: int = 6) -> list[dict]:
    return top_memories(conn, player_id, persona_id, "heuristic", limit=limit)


def open_tactical(conn, match_id: str, *, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM tactical_notes
           WHERE match_id = ? AND status = 'open'
           ORDER BY created_at, rowid LIMIT ?""",
        (match_id, limit),
    ).fetchall()
    return [{
        "id": row["id"], "match_id": row["match_id"], "kind": row["kind"],
        "text": row["text"], "tags": json.loads(row["tags"] or "[]"),
        "confidence": row["confidence"], "status": row["status"],
        "created_at": row["created_at"],
    } for row in rows]


def render_line(item: dict) -> str:
    notes = [(item.get("observed_at") or "")[:10]]
    confidence = item.get("confidence")
    if confidence == "verified":
        notes.append("历史统计" if item.get("layer") == "profile" else "实测结论")
    elif confidence == "speculated":
        notes.append("推测")
    elif confidence == "disputed":
        notes.append("有争议")
    if "practice" in (item.get("tags") or []):
        notes.append("自主练习")
    notes = [note for note in notes if note]
    return f"- {item['text']}" + (f"（{' / '.join(notes)}）" if notes else "")


def render(items: list[dict]) -> str:
    return "\n".join(render_line(item) for item in items)
