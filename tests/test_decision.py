from __future__ import annotations

from game.decision import (
    build_banter_prompt,
    build_dealer_prompt,
    build_intent_prompt,
    build_policy_prompt,
    build_review_prompt,
    fallback_action,
    normalize_action,
    parse_dealer_decision,
    parse_intent,
    parse_policy,
    parse_review,
    persona_system,
    resolve_intent_action,
)

VIEW = {
    "stage": 1, "dealer_hp": 3, "dealer_max_hp": 4, "player_hp": 2, "player_max_hp": 4,
    "remaining_live_count": 2, "remaining_blank_count": 1,
    "barrel_sawed_off": False, "player_handcuffed": False, "dealer_handcuffed": False,
    "dealer_items": [{"slot": 0, "item": "handsaw", "label": "手锯"},
                     {"slot": 1, "item": "magnifying glass", "label": "放大镜"}],
    "player_items": [{"slot": 0, "item": "beer", "label": "啤酒"}],
}


def test_parse_fenced_json():
    text = """```json
{"inner_thought":"锯枪管","memory_note":"已用锯","game_action":{"type":"use_item","item":"handsaw"},"qq_message":"先锯短一点"}
```"""
    decision = parse_dealer_decision(text, VIEW)
    assert decision["game_action"] == {"type": "use_item", "item": "handsaw"}
    assert decision["fallback"] is False
    assert decision["qq_message"] == "先锯短一点"


def test_invalid_action_falls_back_legally():
    decision = parse_dealer_decision(
        '{"game_action":{"type":"use_item","item":"火箭筒"},"qq_message":"x"}', VIEW)
    assert decision["fallback"] is True
    assert normalize_action(decision["game_action"], VIEW) is not None
    assert fallback_action(VIEW)["type"] in ("use_item", "shoot")


def test_item_and_target_aliases():
    assert normalize_action({"type": "use_item", "item": "手锯"}, VIEW) == {
        "type": "use_item", "item": "handsaw"}
    assert normalize_action({"type": "shoot", "target": "玩家"}, VIEW) == {
        "type": "shoot", "target": "player"}
    assert normalize_action({"type": "shoot", "target": "自己"}, VIEW) == {
        "type": "shoot", "target": "self"}


def test_policy_parse_and_fallback():
    system, user = build_policy_prompt("人设", "别用记忆，正常打", {"matches": 3})
    assert "off" in system and "别用记忆" in user
    policy = parse_policy('{"mode":"off","learn":false,"reason":"公平","opening_line":"来"}')
    assert policy["mode"] == "off" and policy["learn"] is False
    fallback = parse_policy("不是 JSON")
    assert fallback["mode"] == "adaptive" and fallback["learn"] is True


def test_dealer_prompts_keep_persona_in_first_person():
    system = persona_system("你是小小鱼，玩家的女儿。", "小小鱼")
    assert "亲自扮演" in system and "不是旁观者" in system
    assert "小小鱼" in system and "不要为玩家加油" in system

    banter_system, banter_user = build_banter_prompt(
        "你是小小鱼，玩家的女儿。", "player_used_item",
        {"stage": 1, "dealer_hp": 3, "player_hp": 3,
         "remaining_live_count": 2, "remaining_blank_count": 1},
        "", "adaptive", persona_name="小小鱼")
    assert "庄家" in banter_system and "第一人称" in banter_user
    assert "坏恶魔" in banter_system  # explicit anti-example

    dealer_user = build_dealer_prompt("局面", "", "adaptive", persona_name="小小鱼")
    assert "第一人称" in dealer_user and "小小鱼" in dealer_user

    review_system, _ = build_review_prompt("记录", "win", persona_name="小小鱼")
    assert "亲自扮演" in review_system and "小小鱼" in review_system


def test_semantic_start_intent_and_confirmation():
    system, user = build_intent_prompt(
        "你是小小鱼，玩家的女儿。", "小小鱼", "今天有点无聊，陪我玩会儿？")
    assert "不要依赖固定关键词" in system
    assert "restart" in system and "confirm" in system and "other" in system
    assert "今天有点无聊" in user

    parsed = parse_intent(
        '{"intent":"start","confidence":0.72,"reply":"要我现在摆桌吗？"}')
    assert parsed == {"intent": "start", "confidence": 0.72,
                      "reply": "要我现在摆桌吗？"}
    assert parse_intent("不是 JSON")["intent"] == "other"
    assert parse_intent('{"intent":"START","confidence":9}')["confidence"] == 1.0
    assert parse_intent('{"intent":"restart","confidence":0.9}')["intent"] == "restart"

    assert resolve_intent_action("start", 0.95, False) == "start_now"
    assert resolve_intent_action("start", 0.7, False) == "start_now"
    assert resolve_intent_action("start", 0.45, False) == "ask"
    assert resolve_intent_action("start", 0.3, False) == "ignore"
    assert resolve_intent_action("restart", 0.95, False, active_match=True) == "start_now"
    assert resolve_intent_action("restart", 0.7, False, active_match=True) == "ask"
    assert resolve_intent_action("start", 0.95, False, active_match=True) == "start_now"
    assert resolve_intent_action("confirm", 0.8, True) == "start_now"
    assert resolve_intent_action("confirm", 0.8, False) == "ignore"
    assert resolve_intent_action("decline", 0.8, True) == "decline"
    assert resolve_intent_action("other", 1.0, True) == "ignore"


def test_review_parse():
    review = parse_review("""{
      "qq_message":"你比上次更敢赌了",
      "profile":[{"text":"玩家在残血时喜欢自射","confidence":"verified","evidence":"第2局"}],
      "heuristics":[{"text":"空包多时保留手铐","confidence":"speculated"}]
    }""")
    assert review["profile"][0]["text"].startswith("玩家")
    assert review["heuristics"][0]["confidence"] == "speculated"
