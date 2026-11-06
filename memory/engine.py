"""Buckshot 内聚记忆核。

三层记忆：
  1. tactical_notes：本局算牌/观察/目标，单局有效；new_loadout 清掉枪膛位置类条目；
  2. profile：对某个 QQ 玩家的跨局心理画像；
  3. heuristic：AI 自己的恶魔博弈法则，可跨人格复用。

player_id 由插件用平台+QQ 号生成；persona_id 只用于区分偏好和出处。
"""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import db, recall as recall_mod
from .util import estimate_tokens, index_tokens, new_id, norm_statement

LAYERS = ("profile", "heuristic")
CONFIDENCES = ("verified", "speculated", "disputed")
MODE_INSTRUCTIONS = {
    "off": "本次对局关闭跨局记忆。只依据当前局面与规则，不要使用对手画像或历史经验。",
    "on": "本次对局启用跨局记忆。尽量利用对手画像与已验证的博弈法则提高胜率。",
    "adaptive": "跨局记忆仅供参考。是否采纳由你的人设与当前心情自行决定：想赢就用，想放水可以忽略。",
}
ITEM_LABELS = {
    "handsaw": "手锯",
    "magnifying glass": "放大镜",
    "beer": "啤酒",
    "cigarettes": "香烟",
    "handcuffs": "手铐",
    "burner phone": "一次性手机",
    "expired medicine": "过期药",
    "inverter": "逆转器",
    "adrenaline": "肾上腺素",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _rank(confidence: str) -> int:
    return {"verified": 2, "speculated": 1, "disputed": 0}.get(confidence, 1)


class BuckshotMemory:
    def __init__(self, path: str | Path, *, token_counter: Callable[[str], int] = estimate_tokens):
        self.path = str(path)
        self.conn = db.connect(self.path)
        self.token_counter = token_counter
        self._lock = threading.RLock()
        self._depth = 0

    # ------------------------------------------------------------ 基础设施

    @contextmanager
    def _tx(self):
        with self._lock:
            if self._depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            else:
                self.conn.execute(f"SAVEPOINT sp{self._depth}")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                else:
                    self.conn.execute(f"ROLLBACK TO sp{self._depth}")
                    self.conn.execute(f"RELEASE sp{self._depth}")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("COMMIT")
                else:
                    self.conn.execute(f"RELEASE sp{self._depth}")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def now(self) -> str:
        return _now_iso()

    def _index_memory(self, memory_id: str) -> None:
        row = self.conn.execute("SELECT text, tags FROM memories WHERE id = ?", (memory_id,)).fetchone()
        self.conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        if row is None:
            return
        tokens = index_tokens(row["text"] + " " + " ".join(json.loads(row["tags"] or "[]")))
        self.conn.execute("INSERT INTO memory_fts (memory_id, tokens) VALUES (?,?)",
                          (memory_id, " ".join(tokens)))

    def _index_note(self, note_id: str) -> None:
        row = self.conn.execute("SELECT text, tags FROM tactical_notes WHERE id = ?", (note_id,)).fetchone()
        self.conn.execute("DELETE FROM tactical_fts WHERE note_id = ?", (note_id,))
        if row is None:
            return
        tokens = index_tokens(row["text"] + " " + " ".join(json.loads(row["tags"] or "[]")))
        self.conn.execute("INSERT INTO tactical_fts (note_id, tokens) VALUES (?,?)",
                          (note_id, " ".join(tokens)))

    # ------------------------------------------------------------ 对局

    def start_match(self, player_id: str, persona_id: str | None = None, *,
                    memory_mode: str = "adaptive") -> str:
        with self._lock, self._tx():
            now = self.now()
            previous = self.conn.execute(
                "SELECT id FROM matches WHERE player_id = ? AND ended_at IS NULL", (player_id,)
            ).fetchall()
            for row in previous:
                self.conn.execute(
                    "UPDATE matches SET ended_at=?, result='aborted' WHERE id=?", (now, row["id"]))
                self.conn.execute(
                    "UPDATE tactical_notes SET status='archived' WHERE match_id=? AND status='open'",
                    (row["id"],))
            match_id = new_id("match")
            self.conn.execute(
                """INSERT INTO matches (id, player_id, persona_id, memory_mode, started_at)
                   VALUES (?,?,?,?,?)""",
                (match_id, player_id, persona_id, memory_mode, now))
            return match_id

    def set_match_mode(self, match_id: str, mode: str) -> None:
        with self._lock, self._tx():
            self.conn.execute("UPDATE matches SET memory_mode=? WHERE id=?", (mode, match_id))

    def end_match(self, match_id: str, *, result: str | None = None, score: str | None = None,
                  meta: dict | None = None) -> None:
        with self._lock, self._tx():
            now = self.now()
            self.conn.execute(
                "UPDATE matches SET ended_at=?, result=?, score=?, meta=? WHERE id=?",
                (now, result, score, json.dumps(meta or {}, ensure_ascii=False), match_id))
            self.conn.execute(
                "UPDATE tactical_notes SET status='archived' WHERE match_id=? AND status='open'",
                (match_id,))

    def new_loadout(self, match_id: str) -> int:
        """新一轮装填：作废所有和具体枪膛位置有关的便条，保留对玩家风格的观察。"""
        with self._lock, self._tx():
            return self.conn.execute(
                """UPDATE tactical_notes SET status='archived'
                   WHERE match_id=? AND status='open' AND kind IN ('shell','private')""",
                (match_id,)).rowcount

    def add_tactical(self, match_id: str, text: str, *, kind: str = "observation",
                     player_id: str | None = None, confidence: str = "verified",
                     evidence: str | None = None, tags=()) -> dict:
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:300]
        if not text:
            raise ValueError("便条不能为空")
        if kind not in ("shell", "observation", "goal", "private"):
            raise ValueError("未知战术便条类型")
        if confidence not in CONFIDENCES:
            raise ValueError("未知置信度")
        with self._lock, self._tx():
            existing = self.conn.execute(
                """SELECT * FROM tactical_notes WHERE match_id=? AND kind=? AND status='open'""",
                (match_id, kind)).fetchall()
            for row in existing:
                if norm_statement(row["text"]) == norm_statement(text):
                    if _rank(confidence) > _rank(row["confidence"]):
                        self.conn.execute(
                            "UPDATE tactical_notes SET confidence=? WHERE id=?",
                            (confidence, row["id"]))
                    return {"id": row["id"], "status": "refreshed"}
            note_id = new_id("t")
            self.conn.execute(
                """INSERT INTO tactical_notes
                   (id, match_id, player_id, kind, text, tags, confidence, status, created_at)
                   VALUES (?,?,?,?,?,?,?, 'open', ?)""",
                (note_id, match_id, player_id, kind, text,
                 json.dumps(list(tags), ensure_ascii=False), confidence, self.now()))
            self._index_note(note_id)
            return {"id": note_id, "status": "created"}

    def add_profile(self, player_id: str, text: str, *, confidence: str = "speculated",
                    evidence: str | None = None, match_id: str | None = None,
                    persona_id: str | None = None, tags=("history",)) -> dict:
        return self._add_memory("profile", player_id, text, persona_id=persona_id,
                                confidence=confidence, evidence=evidence, match_id=match_id, tags=tags)

    def add_heuristic(self, text: str, *, confidence: str = "speculated",
                      evidence: str | None = None, match_id: str | None = None,
                      persona_id: str | None = None, tags=("practice",)) -> dict:
        return self._add_memory("heuristic", None, text, persona_id=persona_id,
                                confidence=confidence, evidence=evidence, match_id=match_id, tags=tags)

    def _add_memory(self, layer: str, player_id: str | None, text: str, *, persona_id,
                    confidence: str, evidence: str | None, match_id: str | None, tags) -> dict:
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:400]
        if not text:
            raise ValueError("记忆陈述不能为空")
        if confidence not in CONFIDENCES:
            raise ValueError("未知置信度")
        with self._lock, self._tx():
            rows = self.conn.execute(
                """SELECT * FROM memories WHERE layer=? AND status IN ('active','disputed')
                   AND (player_id IS ?)""", (layer, player_id)).fetchall()
            for row in rows:
                if norm_statement(row["text"]) == norm_statement(text):
                    new_confidence = confidence if _rank(confidence) > _rank(row["confidence"]) \
                        else row["confidence"]
                    new_status = "disputed" if new_confidence == "disputed" else "active"
                    self.conn.execute(
                        """UPDATE memories SET confidence=?, status=?, evidence=?, observed_at=?,
                           updated_at=?, importance=? WHERE id=?""",
                        (new_confidence, new_status, evidence or row["evidence"], self.now(),
                         self.now(), 3 if new_confidence == "verified" else row["importance"],
                         row["id"]))
                    return {"id": row["id"], "status": "refreshed", "confidence": new_confidence}
            memory_id = new_id("m")
            now = self.now()
            self.conn.execute(
                """INSERT INTO memories
                   (id, layer, player_id, persona_id, text, tags, confidence, status,
                    evidence, match_id, importance, observed_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (memory_id, layer, player_id, persona_id, text,
                 json.dumps(list(tags), ensure_ascii=False), confidence,
                 "disputed" if confidence == "disputed" else "active",
                 evidence, match_id, 3 if confidence == "verified" else 2,
                 _today(), now, now))
            self._index_memory(memory_id)
            return {"id": memory_id, "status": "created", "confidence": confidence}

    def commit_review(self, player_id: str, match_id: str, *,
                      profile: list, heuristics: list) -> dict:
        """把复盘结果写入长期层。条目可以是字符串或 {text,confidence,evidence}。"""
        out = {"profile": [], "heuristic": []}
        for item in profile or []:
            text, confidence, evidence = self._review_item(item)
            if text:
                out["profile"].append(self.add_profile(
                    player_id, text, confidence=confidence, evidence=evidence,
                    match_id=match_id, tags=("history",)))
        for item in heuristics or []:
            text, confidence, evidence = self._review_item(item)
            if text:
                out["heuristic"].append(self.add_heuristic(
                    text, confidence=confidence, evidence=evidence,
                    match_id=match_id, tags=("practice", "review")))
        return out

    @staticmethod
    def _review_item(item) -> tuple[str, str, str | None]:
        if isinstance(item, str):
            return item.strip(), "speculated", None
        if isinstance(item, dict):
            confidence = item.get("confidence") or "speculated"
            if confidence not in CONFIDENCES:
                confidence = "speculated"
            return str(item.get("text") or "").strip(), confidence, item.get("evidence")
        return "", "speculated", None

    # ------------------------------------------------------------ 读

    def policy_get(self, player_id: str, persona_key: str = "*") -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM policies WHERE player_id=? AND persona_key=?",
                (player_id, persona_key)).fetchone()
            return dict(row) if row else None

    def policy_set(self, player_id: str, persona_key: str, *, mode: str, learn: bool,
                   reason: str | None = None) -> dict:
        if mode not in ("auto", "off", "on", "adaptive"):
            raise ValueError("mode 必须是 auto/off/on/adaptive")
        with self._lock, self._tx():
            self.conn.execute(
                """INSERT INTO policies (player_id, persona_key, mode, learn, reason, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT (player_id, persona_key) DO UPDATE SET
                     mode=excluded.mode, learn=excluded.learn,
                     reason=excluded.reason, updated_at=excluded.updated_at""",
                (player_id, persona_key, mode, int(bool(learn)), reason, self.now()))
        return self.policy_get(player_id, persona_key) or {}

    def stats(self, player_id: str, persona_key: str = "*") -> dict:
        with self._lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM matches WHERE player_id=? AND ended_at IS NOT NULL",
                (player_id,)).fetchone()[0]
            wins = self.conn.execute(
                "SELECT COUNT(*) FROM matches WHERE player_id=? AND result='win'",
                (player_id,)).fetchone()[0]
            losses = self.conn.execute(
                "SELECT COUNT(*) FROM matches WHERE player_id=? AND result='loss'",
                (player_id,)).fetchone()[0]
            profiles = recall_mod.profile_for(self.conn, player_id, persona_key, limit=8)
            heuristics = recall_mod.heuristics_for(self.conn, None, persona_key, limit=8)
            return {"matches": total, "wins": wins, "losses": losses,
                    "profile": profiles, "heuristics": heuristics,
                    "policy": self.policy_get(player_id, persona_key)}

    def match_notes(self, match_id: str, *, include_archived: bool = True) -> list[dict]:
        with self._lock:
            sql = "SELECT * FROM tactical_notes WHERE match_id=?"
            args = [match_id]
            if not include_archived:
                sql += " AND status='open'"
            sql += " ORDER BY created_at, rowid"
            return [dict(row) for row in self.conn.execute(sql, args)]

    def build_context(self, match_id: str, player_id: str, persona_id: str | None,
                      query: str, *, mode: str = "adaptive", token_budget: int = 600) -> dict:
        """生成 <记忆>：战术便条 + 对手画像 + 博弈法则，默认 600 token。"""
        mode = mode if mode in MODE_INSTRUCTIONS else "adaptive"
        with self._lock:
            notes = recall_mod.open_tactical(self.conn, match_id)
            memory_sets = {"profile": [], "heuristic": []}
            if mode != "off":
                memory_sets = recall_mod.relevant(
                    self.conn, player_id, persona_id, query, team=True,
                    limit=4 if mode == "adaptive" else 6)
                if mode == "adaptive":
                    memory_sets["profile"] = memory_sets.get("profile", [])[:2]
                    memory_sets["heuristic"] = memory_sets.get("heuristic", [])[:2]

        count = self.token_counter
        shares = {"notes": 180, "profile": 180, "heuristic": 240}
        scale = token_budget / sum(shares.values())
        allowance = {"notes": int(shares["notes"] * scale)}
        allowance["profile"] = allowance["notes"] + int(shares["profile"] * scale)
        allowance["heuristic"] = token_budget
        used = 0

        def take(items, render, section, limit=None):
            nonlocal used
            out = []
            for item in items:
                cost = count(render(item)) + 2
                if used + cost > allowance[section] or (limit is not None and len(out) >= limit):
                    break
                out.append(item)
                used += cost
            return out

        def note_line(note: dict) -> str:
            kind = {"shell": "本局算牌", "private": "本局验弹", "goal": "本局目标"}.get(
                note["kind"], "本局观察")
            suffix = kind
            if note["confidence"] == "speculated":
                suffix += " / 推测"
            elif note["confidence"] == "disputed":
                suffix += " / 有争议"
            return f"- {note['text']}（{suffix}）"

        notes = take(notes, note_line, "notes", limit=8)
        profiles = take(memory_sets.get("profile", []), recall_mod.render_line, "profile", limit=3)
        heuristics = take(memory_sets.get("heuristic", []), recall_mod.render_line, "heuristic", limit=4)
        if not (notes or profiles or heuristics):
            return {"text": "", "budget_used": 0, "notes": notes,
                    "profile": profiles, "heuristic": heuristics}
        parts = ["<记忆>"]
        if notes:
            parts += ["【本局算牌与战术便条 (Tactical Scratchpad)】",
                      "\n".join(note_line(n) for n in notes)]
        if profiles:
            parts += ["【对手心理画像 (Player Profile - 跨局积累)】",
                      recall_mod.render(profiles)]
        if heuristics:
            parts += ["【恶魔博弈法则 (Strategic Heuristics)】",
                      recall_mod.render(heuristics)]
        parts.append("</记忆>")
        return {"text": "\n".join(parts), "budget_used": used, "notes": notes,
                "profile": profiles, "heuristic": heuristics}

    # ------------------------------------------------------------ 视图与提示

    @staticmethod
    def render_dealer_view(state: dict) -> str:
        """只渲染合法视野；绝不读取 state['sequence'] 或玩家私有验弹。"""
        live = int(state.get("remaining_live_count", 0))
        blank = int(state.get("remaining_blank_count", 0))
        total = live + blank
        prob = (live / total * 100) if total else 0.0
        stage = state.get("stage", 1)
        dealer_hp = state.get("dealer_hp", 0)
        dealer_max = state.get("dealer_max_hp", dealer_hp or 1)
        player_hp = state.get("player_hp", 0)
        player_max = state.get("player_max_hp", player_hp or 1)
        lines = [
            f'<恶魔当前视野 stage="{stage}" dealer_hp="{dealer_hp}/{dealer_max}" '
            f'player_hp="{player_hp}/{player_max}">',
            f"【弹仓公开算牌】本轮剩余：实弹 {live} 发 | 空包弹 {blank} 发"
            f"（实弹概率 {prob:.1f}%）",
            f"【桌面状态】枪管已锯断: {'是' if state.get('barrel_sawed_off') else '否'}"
            f" | 玩家被手铐禁锢: {'是' if state.get('player_handcuffed') else '否'}"
            f" | 恶魔被手铐禁锢: {'是' if state.get('dealer_handcuffed') else '否'}",
            "【我方(恶魔)道具】" + (BuckshotMemory._render_items(state.get("dealer_items")) or "（无）"),
            "【对方(玩家)道具】" + (BuckshotMemory._render_items(state.get("player_items")) or "（无）"),
        ]
        if state.get("current_chamber_shell"):
            lines.append(f"【已验证枪膛】当前第 1 发：{BuckshotMemory._shell_label(state['current_chamber_shell'])}")
        future = state.get("known_future") or []
        if future:
            lines.append("【已验证未来】" + " | ".join(
                f"第 {int(item.get('offset', 0))} 发：{BuckshotMemory._shell_label(item.get('shell'))}"
                for item in future if item.get("shell")))
        lines.append("</恶魔当前视野>")
        return "\n".join(lines)

    @staticmethod
    def _shell_label(shell) -> str:
        value = str(shell or "").upper()
        if value in ("LIVE", "实弹"):
            return "实弹 (LIVE)"
        if value in ("BLANK", "空包弹"):
            return "空包弹 (BLANK)"
        return str(shell)

    @staticmethod
    def _render_items(items) -> str:
        out = []
        for index, item in enumerate(items or []):
            if isinstance(item, dict):
                name = str(item.get("item") or item.get("name") or "")
                slot = item.get("slot", index)
                label = item.get("label") or ITEM_LABELS.get(name, name)
                out.append(f"[{slot}] {label}({name})")
            else:
                name = str(item)
                out.append(f"[{index}] {ITEM_LABELS.get(name, name)}({name})")
        return " | ".join(out)

    @staticmethod
    def policy_instruction(mode: str) -> str:
        return MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["adaptive"])

    @staticmethod
    def item_rule_text() -> str:
        return (
            "道具规则：手锯让下一枪伤害+1；放大镜查看当前枪膛；啤酒退掉当前子弹；"
            "香烟回复1点；手铐跳过对方下一回合；一次性手机得知未来某一发；"
            "过期药50%回2点/50%扣1点；逆转器翻转当前子弹；肾上腺素可偷对方道具。"
        )
