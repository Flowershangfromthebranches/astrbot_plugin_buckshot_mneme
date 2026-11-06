"""内置迷你《恶魔轮盘赌》引擎。

用于没有 Godot 时的完整链路测试，也用于 AI 自主练习。
这个引擎只产生“合法视野”：dealer_view() 绝不返回 sequence 数组或玩家私有验弹。
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

ITEMS = [
    "handsaw", "magnifying glass", "beer", "cigarettes", "handcuffs",
    "burner phone", "expired medicine", "inverter", "adrenaline",
]
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
ITEM_DESCRIPTIONS = {
    "handsaw": "下一枪伤害 +1",
    "magnifying glass": "查看当前枪膛",
    "beer": "退出当前子弹",
    "cigarettes": "恢复 1 点生命",
    "handcuffs": "跳过对方下一回合",
    "burner phone": "得知未来某一发",
    "expired medicine": "50% 回复 2 点，50% 失去 1 点",
    "inverter": "翻转当前子弹",
    "adrenaline": "偷取对手一件道具",
}


class GameError(ValueError):
    pass


@dataclass
class PlayerState:
    hp: int
    max_hp: int
    items: list[str] = field(default_factory=list)
    cuffed: bool = False
    known_current: str | None = None
    known_future: dict[int, str] = field(default_factory=dict)


class MockBuckshotGame:
    max_stage = 3
    max_items = 8

    def __init__(self, *, seed: int | None = None, player_name: str = "玩家",
                 balanced_hp: bool = True):
        self.rng = random.Random(seed)
        self.player_name = player_name
        self.balanced_hp = balanced_hp
        self.stage = 1
        self.turn = "player"
        self.shells: list[str] = []
        self.player = PlayerState(4, 4)
        self.dealer = PlayerState(4, 4)
        self.barrel_sawed_off = False
        self.damage = 1
        self.game_over = False
        self.result: str | None = None
        self.last_event: str | None = None
        self.log: list[str] = []
        self.started = False

    # ------------------------------------------------------------ 开局 / 新弹仓

    def start(self) -> list[dict]:
        if not self.started:
            self.started = True
            self._begin_stage(1)
            return [{"event": "new_loadout", "state": self.public_state()}] + self._turn_events()
        return self._turn_events()

    def _begin_stage(self, stage: int) -> None:
        self.stage = stage
        player_hp = 3 + (1 if stage >= 3 else 0)
        dealer_hp = 3 + (1 if stage >= 2 else 0)
        if self.balanced_hp:
            # 原版第二关给脚本庄家 +1 血作为补偿；对手换成 LLM 后默认对称。
            dealer_hp = player_hp
        self.player.hp = self.player.max_hp = player_hp
        self.dealer.hp = self.dealer.max_hp = dealer_hp
        self.player.items = []
        self.dealer.items = []
        self.player.cuffed = False
        self.dealer.cuffed = False
        self.player.known_current = None
        self.player.known_future = {}
        self.dealer.known_current = None
        self.dealer.known_future = {}
        self.barrel_sawed_off = False
        self.damage = 1
        self.game_over = False
        self.result = None
        self._load_shells()
        self._deal_items(self.player, 3 + stage)
        self._deal_items(self.dealer, 3 + stage)
        self.turn = "player"
        self.last_event = "new_loadout"
        self._log(f"第 {stage} 阶段开始：{self.shells.count('live')} 实弹 / "
                  f"{self.shells.count('blank')} 空包弹")
        self._begin_player_turn(skip_event=True)

    def _load_shells(self) -> None:
        total = self.rng.randint(2, 8)
        live = self.rng.randint(1, total - 1)
        blank = total - live
        shells = ["LIVE"] * live + ["BLANK"] * blank
        self.rng.shuffle(shells)
        self.shells = shells

    def _deal_items(self, side: PlayerState, count: int) -> None:
        count = min(count, self.max_items)
        pool = list(ITEMS)
        self.rng.shuffle(pool)
        side.items = pool[:count]

    # ------------------------------------------------------------ 合法视野

    def public_state(self) -> dict:
        return {
            "stage": self.stage,
            "turn": self.turn,
            "player_name": self.player_name,
            "dealer_hp": self.dealer.hp,
            "dealer_max_hp": self.dealer.max_hp,
            "player_hp": self.player.hp,
            "player_max_hp": self.player.max_hp,
            "remaining_live_count": self.shells.count("LIVE"),
            "remaining_blank_count": self.shells.count("BLANK"),
            "barrel_sawed_off": self.barrel_sawed_off,
            "player_handcuffed": self.player.cuffed,
            "dealer_handcuffed": self.dealer.cuffed,
            "dealer_items": self._item_view(self.dealer.items),
            "player_items": self._item_view(self.player.items),
            "game_over": self.game_over,
            "result": self.result,
            "last_event": self.last_event,
        }

    def dealer_view(self) -> dict:
        view = self.public_state()
        # 仅暴露通过道具或公开计数能确定的枪膛；绝不暴露 sequence。
        if self.dealer.known_current:
            view["current_chamber_shell"] = self.dealer.known_current
        elif len(self.shells) == 1:
            view["current_chamber_shell"] = self.shells[0]
        elif self.shells and all(shell == "LIVE" for shell in self.shells):
            view["current_chamber_shell"] = "LIVE"
        elif self.shells and all(shell == "BLANK" for shell in self.shells):
            view["current_chamber_shell"] = "BLANK"
        view["known_future"] = [
            {"offset": offset, "shell": shell}
            for offset, shell in sorted(self.dealer.known_future.items())
            if offset > 0 and offset < len(self.shells)
        ]
        return view

    def player_view(self) -> dict:
        view = self.public_state()
        if self.player.known_current:
            view["current_chamber_shell"] = self.player.known_current
        elif len(self.shells) == 1:
            view["current_chamber_shell"] = self.shells[0]
        elif self.shells and all(shell == "LIVE" for shell in self.shells):
            view["current_chamber_shell"] = "LIVE"
        elif self.shells and all(shell == "BLANK" for shell in self.shells):
            view["current_chamber_shell"] = "BLANK"
        view["known_future"] = [
            {"offset": offset, "shell": shell}
            for offset, shell in sorted(self.player.known_future.items())
            if offset > 0 and offset < len(self.shells)
        ]
        return view

    def _item_view(self, items: list[str]) -> list[dict]:
        return [{"slot": index, "item": name, "label": ITEM_LABELS.get(name, name),
                 "description": ITEM_DESCRIPTIONS.get(name, "")}
                for index, name in enumerate(items)]

    # ------------------------------------------------------------ 玩家动作

    def player_action(self, action: dict) -> list[dict]:
        if self.game_over:
            raise GameError("对局已经结束")
        if self.turn != "player":
            raise GameError("现在不是玩家回合")
        kind = action.get("action") or action.get("type")
        events: list[dict] = []
        if kind == "use_item":
            index = int(action.get("slot_index", -1))
            if not 0 <= index < len(self.player.items):
                raise GameError("道具槽位不存在")
            events += self._use_item("player", self.player.items[index])
        elif kind == "shoot":
            target = action.get("target")
            if target not in ("dealer", "self"):
                raise GameError("target 必须是 dealer 或 self")
            events += self._shoot("player", target)
        elif kind == "pickup_gun":
            events.append({"event": "gun_picked_up", "role": "player"})
        else:
            raise GameError("未知玩家动作")
        self._record_events(events)
        if any(item.get("event") == "game_over" for item in events):
            return events
        return events + self._turn_events()

    # ------------------------------------------------------------ 恶魔动作

    def dealer_action(self, action: dict) -> list[dict]:
        if self.game_over:
            raise GameError("对局已经结束")
        if self.turn != "dealer":
            raise GameError("现在不是恶魔回合")
        kind = action.get("action") or action.get("type")
        events: list[dict] = []
        if kind == "use_item":
            item = str(action.get("item") or "")
            if item not in self.dealer.items:
                raise GameError(f"恶魔没有道具 {item}")
            events += self._use_item("dealer", item)
        elif kind == "shoot":
            target = action.get("target")
            if target not in ("player", "self"):
                raise GameError("target 必须是 player 或 self")
            events += self._shoot("dealer", target)
        else:
            raise GameError("未知恶魔动作")
        self._record_events(events)
        if any(item.get("event") == "game_over" for item in events):
            return events
        return events + self._turn_events()

    # ------------------------------------------------------------ 回合

    def _turn_events(self) -> list[dict]:
        if self.game_over:
            return [{"event": "game_over", "result": self.result,
                     "state": self.public_state()}]
        if self.turn == "dealer":
            return [{"event": "dealer_turn_request", "view": self.dealer_view()}]
        return [{"event": "player_turn", "state": self.public_state()}]

    def _begin_player_turn(self, *, skip_event: bool = False) -> None:
        if self.game_over:
            return
        if self.player.cuffed:
            self.player.cuffed = False
            self.last_event = "player_handcuffed_skip"
            self._log("玩家被手铐限制，跳过本回合")
            self._begin_dealer_turn()
            return
        self.turn = "player"
        if not skip_event:
            self.last_event = "player_turn"

    def _begin_dealer_turn(self) -> None:
        if self.game_over:
            return
        if self.dealer.cuffed:
            self.dealer.cuffed = False
            self.last_event = "dealer_handcuffed_skip"
            self._log("恶魔被手铐限制，跳过本回合")
            self._begin_player_turn()
            return
        self.turn = "dealer"
        self.last_event = "dealer_turn"
        self._log("轮到恶魔")

    # ------------------------------------------------------------ 道具

    def _use_item(self, side: str, item: str) -> list[dict]:
        me = self.player if side == "player" else self.dealer
        other = self.dealer if side == "player" else self.player
        if item not in me.items:
            raise GameError(f"没有道具 {item}")
        me.items.remove(item)
        events: list[dict] = [{"event": f"{side}_used_item", "role": side, "item": item,
                               "label": ITEM_LABELS.get(item, item)}]
        self.last_event = f"{side}_used_{item.replace(' ', '_')}"
        if item == "handsaw":
            self.barrel_sawed_off = True
            self.damage = 2
            self._log(f"{'玩家' if side == 'player' else '恶魔'}锯短了枪管")
        elif item == "magnifying glass":
            shell = self.shells[0] if self.shells else None
            me.known_current = shell
            if side == "player":
                events.append(self._private_peek("magnifying_glass", shell, "current"))
        elif item == "beer":
            shell = self._eject_current()
            if side == "player":
                events.append(self._private_peek("beer", shell, "current"))
            if shell:
                self._log(f"{'玩家' if side == 'player' else '恶魔'}用啤酒退出一发"
                          f"{'实弹' if shell == 'LIVE' else '空包弹'}")
            if not self.shells:
                events += self._new_loadout_events()
        elif item == "cigarettes":
            me.hp = min(me.max_hp, me.hp + 1)
            self._log(f"{'玩家' if side == 'player' else '恶魔'}抽了香烟，回复 1 点")
        elif item == "handcuffs":
            other.cuffed = True
            self._log(f"{'玩家' if side == 'player' else '恶魔'}铐住了对方")
        elif item == "burner phone":
            if len(self.shells) > 1:
                offset = self.rng.randint(1, len(self.shells) - 1)
                shell = self.shells[offset]
                me.known_future[offset] = shell
                if side == "player":
                    events.append(self._private_peek("burner_phone", shell, "future", offset))
                self._log(f"{'玩家' if side == 'player' else '恶魔'}用手机得知第 {offset + 1} 发")
        elif item == "expired medicine":
            if self.rng.random() < 0.5:
                me.hp = min(me.max_hp, me.hp + 2)
                self._log(f"{'玩家' if side == 'player' else '恶魔'}吃药回复 2 点")
            else:
                me.hp = max(0, me.hp - 1)
                self._log(f"{'玩家' if side == 'player' else '恶魔'}吃到坏药，失去 1 点")
                if me.hp <= 0:
                    events += self._finish_if_dead()
        elif item == "inverter":
            if self.shells:
                self.shells[0] = "BLANK" if self.shells[0] == "LIVE" else "LIVE"
                for holder in (self.player, self.dealer):
                    if holder.known_current:
                        holder.known_current = "BLANK" if holder.known_current == "LIVE" else "LIVE"
                self._log(f"{'玩家' if side == 'player' else '恶魔'}使用了逆转器")
        elif item == "adrenaline":
            if other.items:
                stolen = self.rng.choice(other.items)
                if len(me.items) < self.max_items:
                    other.items.remove(stolen)
                    me.items.append(stolen)
                    self._log(f"{'玩家' if side == 'player' else '恶魔'}偷走了 {ITEM_LABELS.get(stolen, stolen)}")
                else:
                    me.items.append(item)  # 归还，避免凭空消失
                    raise GameError("道具栏已满，无法偷取")
        return events

    def _private_peek(self, source: str, shell: str | None, position: str,
                      offset: int = 0) -> dict:
        if not shell:
            info = "枪膛里已经没有子弹了"
        elif position == "current":
            info = f"当前枪膛内为：{'实弹 (LIVE)' if shell == 'LIVE' else '空包弹 (BLANK)'}"
        else:
            info = f"未来第 {offset + 1} 发是：{'实弹 (LIVE)' if shell == 'LIVE' else '空包弹 (BLANK)'}"
        return {"event": "private_peek", "role": "player", "source": source,
                "shell": shell, "offset": offset, "info": info}

    # ------------------------------------------------------------ 开枪

    def _shoot(self, side: str, target: str) -> list[dict]:
        if not self.shells:
            return self._new_loadout_events()
        shell = self.shells.pop(0)
        self._shift_known_after_pop()
        damage = self.damage
        self.damage = 1
        self.barrel_sawed_off = False
        events: list[dict] = []
        self._log(f"{'玩家' if side == 'player' else '恶魔'}朝"
                  f"{'自己' if target in ('self',) or (side == 'player' and target == 'dealer' and False) else '对方'}"
                  f"开了一枪：{'实弹' if shell == 'LIVE' else '空包弹'}")
        if side == "player":
            if target == "self":
                if shell == "LIVE":
                    self.player.hp = max(0, self.player.hp - damage)
                    if self.player.hp <= 0:
                        events.append({"event": "player_shot_self_live", "shell": shell,
                                       "damage": damage})
                        events += self._finish_if_dead()
                    else:
                        self._begin_dealer_turn()
                        events.append({"event": "player_shot_self_live", "shell": shell,
                                       "damage": damage, "state": self.public_state()})
                else:
                    self._begin_player_turn(skip_event=True)
                    events.append({"event": "player_shot_self_blank", "shell": shell,
                                   "state": self.public_state()})
            else:
                if shell == "LIVE":
                    self.dealer.hp = max(0, self.dealer.hp - damage)
                    if self.dealer.hp <= 0:
                        events.append({"event": "player_shot_dealer_live", "shell": shell,
                                       "damage": damage})
                        events += self._finish_if_dead()
                    else:
                        self._begin_dealer_turn()
                        events.append({"event": "player_shot_dealer_live", "shell": shell,
                                       "damage": damage, "state": self.public_state()})
                else:
                    self._begin_dealer_turn()
                    events.append({"event": "player_shot_dealer_blank", "shell": shell,
                                   "state": self.public_state()})
        else:
            if target == "self":
                if shell == "LIVE":
                    self.dealer.hp = max(0, self.dealer.hp - damage)
                    if self.dealer.hp <= 0:
                        events.append({"event": "dealer_shot_self_live", "shell": shell,
                                       "damage": damage})
                        events += self._finish_if_dead()
                    else:
                        self._begin_player_turn()
                        events.append({"event": "dealer_shot_self_live", "shell": shell,
                                       "damage": damage, "state": self.public_state()})
                else:
                    self._begin_dealer_turn()
                    events.append({"event": "dealer_shot_self_blank", "shell": shell,
                                   "state": self.public_state()})
            else:
                if shell == "LIVE":
                    self.player.hp = max(0, self.player.hp - damage)
                    if self.player.hp <= 0:
                        events.append({"event": "dealer_shot_player_live", "shell": shell,
                                       "damage": damage})
                        events += self._finish_if_dead()
                    else:
                        self._begin_player_turn()
                        events.append({"event": "dealer_shot_player_live", "shell": shell,
                                       "damage": damage, "state": self.public_state()})
                else:
                    self._begin_player_turn()
                    events.append({"event": "dealer_shot_player_blank", "shell": shell,
                                   "state": self.public_state()})
        if not self.shells and not self.game_over:
            events += self._new_loadout_events()
        return events

    def _eject_current(self) -> str | None:
        if not self.shells:
            return None
        shell = self.shells.pop(0)
        self._shift_known_after_pop()
        return shell

    def _shift_known_after_pop(self) -> None:
        for holder in (self.player, self.dealer):
            holder.known_current = holder.known_future.pop(1, None)
            holder.known_future = {offset - 1: shell for offset, shell in holder.known_future.items()
                                   if offset > 1}

    def _new_loadout_events(self) -> list[dict]:
        # 同一阶段内的下一轮装填；保留血量与阶段，重新发弹。
        self._load_shells()
        self.player.items = []
        self.dealer.items = []
        self._deal_items(self.player, 3 + self.stage)
        self._deal_items(self.dealer, 3 + self.stage)
        self.player.cuffed = False
        self.dealer.cuffed = False
        self.player.known_current = None
        self.player.known_future = {}
        self.dealer.known_current = None
        self.dealer.known_future = {}
        self.barrel_sawed_off = False
        self.damage = 1
        self.turn = "player"
        self.last_event = "new_loadout"
        self._log(f"重新装填：{self.shells.count('LIVE')} 实弹 / "
                  f"{self.shells.count('BLANK')} 空包弹")
        return [{"event": "new_loadout", "state": self.public_state()}]

    # ------------------------------------------------------------ 结束

    def _finish_if_dead(self) -> list[dict]:
        events = []
        if self.dealer.hp <= 0:
            self._log(f"恶魔倒下，第 {self.stage} 阶段胜利")
            events.append({"event": "round_win", "stage": self.stage,
                           "state": self.public_state()})
            if self.stage >= self.max_stage:
                self.game_over = True
                self.result = "win"
                self.last_event = "game_over"
                self._log("你赢下了恶魔轮盘赌")
                events.append({"event": "game_over", "result": "win",
                               "state": self.public_state()})
            else:
                self._begin_stage(self.stage + 1)
                events.append({"event": "new_loadout", "state": self.public_state()})
                events.append({"event": "player_turn", "state": self.public_state()})
        elif self.player.hp <= 0:
            self.game_over = True
            self.result = "loss"
            self.last_event = "game_over"
            self._log("玩家倒下，恶魔获胜")
            events.append({"event": "round_lose", "stage": self.stage,
                           "state": self.public_state()})
            events.append({"event": "game_over", "result": "loss",
                           "state": self.public_state()})
        return events

    def _record_events(self, events: list[dict]) -> None:
        pass

    def _log(self, text: str) -> None:
        self.log.append(text)
        self.log = self.log[-80:]
