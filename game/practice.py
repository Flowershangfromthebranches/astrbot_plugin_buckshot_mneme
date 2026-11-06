"""自主练习用的模拟玩家策略。

它只使用玩家视角（公有计数 + 玩家自己的验弹），不会偷看 dealer_view。
"""
from __future__ import annotations


def _slot(items: list[dict], name: str) -> int | None:
    for item in items:
        if (item.get("item") or item.get("name")) == name:
            return int(item.get("slot", 0))
    return None


def choose_player_action(view: dict) -> dict:
    items = list(view.get("player_items") or [])
    live = int(view.get("remaining_live_count", 0))
    blank = int(view.get("remaining_blank_count", 0))
    total = live + blank
    if total <= 0:
        return {"action": "shoot", "target": "dealer"}
    current = str(view.get("current_chamber_shell") or "").upper()
    sawed = bool(view.get("barrel_sawed_off"))
    hp = int(view.get("player_hp", 0))
    max_hp = int(view.get("player_max_hp", hp))
    dealer_cuffed = bool(view.get("dealer_handcuffed"))

    if current == "LIVE":
        slot = _slot(items, "handsaw")
        if slot is not None and not sawed:
            return {"action": "use_item", "slot_index": slot}
        slot = _slot(items, "handcuffs")
        if slot is not None and not dealer_cuffed:
            return {"action": "use_item", "slot_index": slot}
        return {"action": "shoot", "target": "dealer"}
    if current == "BLANK":
        slot = _slot(items, "inverter")
        if slot is not None:
            return {"action": "use_item", "slot_index": slot}
        return {"action": "shoot", "target": "self"}

    if hp <= max_hp - 1:
        slot = _slot(items, "cigarettes")
        if slot is not None:
            return {"action": "use_item", "slot_index": slot}
    if total > 1:
        slot = _slot(items, "magnifying glass")
        if slot is not None:
            return {"action": "use_item", "slot_index": slot}
        slot = _slot(items, "burner phone")
        if slot is not None and total > 2:
            return {"action": "use_item", "slot_index": slot}
    if total > 1 and blank >= live:
        slot = _slot(items, "beer")
        if slot is not None:
            return {"action": "use_item", "slot_index": slot}
    slot = _slot(items, "adrenaline")
    if slot is not None:
        return {"action": "use_item", "slot_index": slot}
    if live <= 0:
        return {"action": "shoot", "target": "self"}
    if blank <= 0:
        return {"action": "shoot", "target": "dealer"}
    return {"action": "shoot", "target": "self" if blank > live else "dealer"}
