from __future__ import annotations

import pytest

from memory.engine import BuckshotMemory


@pytest.fixture()
def memory(tmp_path):
    m = BuckshotMemory(tmp_path / "mneme_buckshot.db")
    yield m
    m.close()


def test_three_layers_and_budget(memory):
    match = memory.start_match("qq:1", "demon", memory_mode="on")
    memory.add_tactical(match, "刚才用手机得知第2发是实弹", kind="private")
    memory.add_profile("qq:1", "玩家在1实1空时更常朝自己开枪", confidence="verified")
    memory.add_heuristic("确认当前为实弹且持有手锯时，先锯枪管", confidence="verified",
                         tags=("practice",))
    context = memory.build_context(match, "qq:1", "demon", "实弹 手锯 玩家", mode="on")
    assert context["text"].startswith("<记忆>")
    assert "Tactical Scratchpad" in context["text"]
    assert "Player Profile" in context["text"]
    assert "Strategic Heuristics" in context["text"]
    assert context["budget_used"] <= 600
    assert "2026-" in context["text"] or "2025-" in context["text"]


def test_off_mode_has_only_tactical(memory):
    match = memory.start_match("qq:1", "demon", memory_mode="off")
    memory.add_tactical(match, "当前只剩1发，已知是实弹", kind="shell")
    memory.add_profile("qq:1", "玩家喜欢赌自射", confidence="speculated")
    memory.add_heuristic("不要在有实弹时朝自己开枪", confidence="verified")
    text = memory.build_context(match, "qq:1", "demon", "实弹", mode="off")["text"]
    assert "Tactical Scratchpad" in text
    assert "Player Profile" not in text
    assert "Strategic Heuristics" not in text
    assert "玩家喜欢赌自射" not in text


def test_new_loadout_clears_shell_but_keeps_observation(memory):
    match = memory.start_match("qq:1", "demon")
    memory.add_tactical(match, "当前枪膛是实弹", kind="shell")
    memory.add_tactical(match, "玩家连续两次先深呼吸再开枪", kind="observation")
    memory.new_loadout(match)
    notes = memory.build_context(match, "qq:1", "demon", "实弹", mode="off")["notes"]
    texts = [n["text"] for n in notes]
    assert "当前枪膛是实弹" not in texts
    assert "玩家连续两次先深呼吸再开枪" in texts


def test_player_isolation_and_fts(memory):
    a = memory.start_match("qq:a", "demon")
    b = memory.start_match("qq:b", "demon")
    memory.add_profile("qq:a", "A 玩家喜欢先用手锯", confidence="verified")
    memory.add_profile("qq:b", "B 玩家喜欢先喝啤酒", confidence="verified")
    text_a = memory.build_context(a, "qq:a", "demon", "手锯", mode="on")["text"]
    text_b = memory.build_context(b, "qq:b", "demon", "手锯", mode="on")["text"]
    assert "A 玩家" in text_a and "B 玩家" not in text_a
    assert "B 玩家" in text_b and "A 玩家" not in text_b


def test_policy_and_stats(memory):
    memory.policy_set("qq:1", "demon", mode="off", learn=False, reason="用户想公平对决")
    policy = memory.policy_get("qq:1", "demon")
    assert policy["mode"] == "off" and policy["learn"] == 0
    memory.start_match("qq:1", "demon")
    memory.add_profile("qq:1", "玩家习惯先观察", confidence="speculated")
    stats = memory.stats("qq:1", "demon")
    assert stats["policy"]["mode"] == "off"
    assert stats["profile"]


def test_new_match_aborts_previous(memory):
    first = memory.start_match("qq:1", "demon")
    second = memory.start_match("qq:1", "demon")
    assert first != second
    row = memory.conn.execute("SELECT result, ended_at FROM matches WHERE id=?", (first,)).fetchone()
    assert row["result"] == "aborted" and row["ended_at"]


def test_render_dealer_view_never_exposes_sequence(memory):
    view = {
        "stage": 2, "dealer_hp": 3, "dealer_max_hp": 4, "player_hp": 2, "player_max_hp": 4,
        "remaining_live_count": 2, "remaining_blank_count": 1,
        "barrel_sawed_off": False, "player_handcuffed": False,
        "dealer_items": [{"slot": 0, "item": "handsaw", "label": "手锯"}],
        "player_items": [{"slot": 0, "item": "beer", "label": "啤酒"}],
        "sequence": ["LIVE", "LIVE", "BLANK"],
        "current_chamber_shell": "LIVE",
        "known_future": [{"offset": 2, "shell": "BLANK"}],
    }
    text = memory.render_dealer_view(view)
    assert "LIVE" in text and "手锯" in text and "啤酒" in text
    assert "sequence" not in text
    assert "['LIVE', 'LIVE', 'BLANK']" not in text


def test_review_commit_and_render_line(memory):
    match = memory.start_match("qq:1", "demon")
    result = memory.commit_review("qq:1", match, profile=[
        {"text": "玩家在残血时会冒险自射", "confidence": "speculated", "evidence": "第3局"},
    ], heuristics=[
        {"text": "空包概率高于实弹时优先自射续回合", "confidence": "verified"},
    ])
    assert result["profile"] and result["heuristic"]
    profile = memory.stats("qq:1", "demon")["profile"]
    assert any("残血" in item["text"] for item in profile)
    line = memory.build_context(match, "qq:1", "demon", "残血 自射", mode="on")["text"]
    assert "推测" in line or "历史统计" in line
