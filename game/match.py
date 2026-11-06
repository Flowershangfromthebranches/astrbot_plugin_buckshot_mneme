"""Mock 对局编排：玩家动作 → 引擎 → 恶魔 LLM 决策 → Wi/QQ 分流。

Godot 模式由 main.py 的桥接消息直接驱动；这里只负责内置模拟引擎。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

from .decision import event_description, fallback_action
from .mock_engine import GameError, MockBuckshotGame

LOG = logging.getLogger(__name__)


class MockMatch:
    def __init__(self, *, match_id: str, player_key: str, player_name: str,
                 memory_mode: str, learn: bool, memory, engine: MockBuckshotGame,
                 ai_decide: Callable[[dict], Awaitable[dict]],
                 send_qq: Callable[[str], Awaitable[None]],
                 send_state: Callable[[dict], Awaitable[None]],
                 send_event: Callable[[dict], Awaitable[None]],
                 send_private: Callable[[dict], Awaitable[None]],
                 on_event: Callable[[dict, dict], Awaitable[None]] | None = None,
                 on_finished: Callable[[dict], Awaitable[None]] | None = None):
        self.match_id = match_id
        self.player_key = player_key
        self.player_name = player_name
        self.memory_mode = memory_mode
        self.learn = learn
        self.memory = memory
        self.engine = engine
        self.ai_decide = ai_decide
        self.send_qq = send_qq
        self.send_state_cb = send_state
        self.send_event_cb = send_event
        self.send_private = send_private
        self.on_event_cb = on_event
        self.on_finished = on_finished
        self.finished = False
        self.dealer_lock = asyncio.Lock()
        self.transcript: list[str] = []
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        events = self.engine.start()
        self.transcript.append(f"[开局] 第 {self.engine.stage} 阶段，"
                               f"{self.engine.shells.count('LIVE')} 实弹 / "
                               f"{self.engine.shells.count('BLANK')} 空包弹")
        await self._handle_events(events)
        if self.engine.turn == "dealer" and not self.finished:
            await self._dealer_loop()

    async def player_action(self, action: dict) -> tuple[bool, str]:
        if self.finished:
            return False, "本局已经结束"
        if self.engine.turn != "player":
            return False, "现在不是你的回合"
        label = self._action_label(action)
        try:
            events = self.engine.player_action(action)
        except GameError as exc:
            return False, str(exc)
        self.transcript.append(f"[玩家] {label}")
        await self._handle_events(events)
        if self.engine.turn == "dealer" and not self.finished:
            await self._dealer_loop()
        return True, "ok"

    async def _dealer_decide(self, view: dict) -> dict:
        """Hard cap on the LLM call so a stuck provider cannot freeze the table."""
        task = asyncio.create_task(self.ai_decide(view))

        def consume(done_task):
            if done_task.cancelled():
                return
            try:
                done_task.exception()
            except Exception:
                pass

        task.add_done_callback(consume)
        done, _ = await asyncio.wait({task}, timeout=50)
        if task not in done:
            task.cancel()
            LOG.error("[buckshot] dealer AI hard timeout; using fallback")
            return {"game_action": fallback_action(view), "qq_message": "", "memory_note": ""}
        try:
            return task.result()
        except Exception:
            LOG.exception("[buckshot] dealer AI failed; using fallback")
            return {"game_action": fallback_action(view), "qq_message": "", "memory_note": ""}

    async def _dealer_loop(self) -> None:
        async with self.dealer_lock:
            guard = 0
            while not self.finished and self.engine.turn == "dealer":
                guard += 1
                if guard > 60:
                    LOG.error("[buckshot] dealer turn exceeded action guard; forcing a shot")
                    try:
                        await self._handle_events(
                            self.engine.dealer_action({"type": "shoot", "target": "player"}))
                    except Exception:
                        LOG.exception("[buckshot] dealer guard action failed")
                        self.finished = True
                    break
                view = self.engine.dealer_view()
                try:
                    decision = await self._dealer_decide(view)
                except Exception:
                    LOG.exception("[buckshot] dealer AI failed; using fallback")
                    decision = {"game_action": fallback_action(view),
                                "qq_message": "", "memory_note": ""}
                message = str(decision.get("qq_message") or "").strip()
                note = str(decision.get("memory_note") or "").strip()
                try:
                    if message:
                        await self.send_qq(message)
                    if note:
                        kind = "shell" if re.search(
                            r"实弹|空包|枪膛|第\s*\d+\s*发|手机|放大镜", note) else "observation"
                        self.memory.add_tactical(
                            self.match_id, note, kind=kind,
                            player_id=self.player_key, confidence="verified")
                except Exception:
                    LOG.exception("[buckshot] dealer side effect failed")
                action = decision.get("game_action") or fallback_action(view)
                self.transcript.append(
                    f"[恶魔] {action.get('type')} {action.get('item') or action.get('target')}"
                    + (f" | {message}" if message else ""))
                try:
                    events = self.engine.dealer_action(action)
                except GameError:
                    events = self.engine.dealer_action(fallback_action(view))
                except Exception:
                    LOG.exception("[buckshot] dealer action failed; using fallback")
                    try:
                        events = self.engine.dealer_action(fallback_action(view))
                    except Exception:
                        LOG.exception("[buckshot] dealer fallback failed; ending turn")
                        self.finished = True
                        break
                try:
                    await self._handle_events(events)
                except Exception:
                    LOG.exception("[buckshot] dealer event dispatch failed")

    async def _handle_events(self, events: list[dict]) -> None:
        for payload in events:
            if not isinstance(payload, dict):
                continue
            name = payload.get("event") or payload.get("type")
            state = payload.get("state")
            if state:
                await self.send_state_cb(state)
            if name == "private_peek":
                # 玩家私有验弹只走 Web，绝不进入 transcript / AI。
                await self.send_private(payload)
                continue
            if name == "new_loadout":
                self.memory.new_loadout(self.match_id)
                self.transcript.append("[装填] 新一轮弹仓")
            if name == "game_over":
                self.finished = True
                self.transcript.append(f"[结算] {payload.get('result')}")
                await self.send_event_cb(payload)
                if self.on_finished is not None:
                    await self.on_finished({"result": payload.get("result"),
                                            "stage": self.engine.stage,
                                            "transcript": "\n".join(self.transcript[-80:])})
                continue
            if name in ("dealer_turn_request",):
                continue
            if name.startswith(("player_shot", "player_used", "round_win", "round_lose",
                                "dealer_shot", "dealer_used")):
                self.transcript.append("[事件] " + event_description(name, payload))
            await self.send_event_cb(payload)
            if self.on_event_cb is not None and name and name.startswith(
                    ("player_shot", "player_used", "round_win", "round_lose")):
                task = asyncio.create_task(self._safe_event(payload))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _safe_event(self, payload: dict) -> None:
        try:
            await self.on_event_cb(payload, self.engine.public_state())
        except Exception:
            LOG.exception("[buckshot] event banter failed")

    @staticmethod
    def _action_label(action: dict) -> str:
        if action.get("action") == "use_item":
            return f"使用道具 slot={action.get('slot_index')}"
        if action.get("action") == "shoot":
            return f"朝{'自己' if action.get('target') == 'self' else '恶魔'}开枪"
        return str(action)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
