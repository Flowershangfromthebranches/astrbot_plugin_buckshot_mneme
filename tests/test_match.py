from __future__ import annotations

import asyncio

from game.decision import fallback_action
from game.match import MockMatch
from game.mock_engine import MockBuckshotGame
from game.practice import choose_player_action
from memory.engine import BuckshotMemory


def test_mock_match_runs_to_finish(tmp_path):
    async def run():
        memory = BuckshotMemory(tmp_path / "db.sqlite")
        match_id = memory.start_match("qq:1", "demon")
        states, events, qqs, finished = [], [], [], []

        async def send_state(state): states.append(state)
        async def send_event(event): events.append(event)
        async def send_private(peek): pass
        async def send_qq(text): qqs.append(text)
        async def ai_decide(view):
            return {"game_action": fallback_action(view), "qq_message": "",
                    "memory_note": "", "fallback": False}
        async def on_finished(info): finished.append(info)

        engine = MockBuckshotGame(seed=9)
        match = MockMatch(
            match_id=match_id, player_key="qq:1", player_name="测试", memory_mode="on",
            learn=True, memory=memory, engine=engine, ai_decide=ai_decide,
            send_qq=send_qq, send_state=send_state, send_event=send_event,
            send_private=send_private, on_finished=on_finished,
        )
        await match.start()
        turns = 0
        while not match.finished and turns < 200:
            turns += 1
            if engine.turn == "player":
                await match.player_action(choose_player_action(engine.player_view()))
            elif engine.turn == "dealer":
                await match._dealer_loop()
        await match.close()
        assert match.finished
        assert finished and finished[0]["result"] in ("win", "loss")
        assert states and "turn" in states[-1]
        assert engine.game_over
        memory.close()

    asyncio.run(run())


def test_match_rejects_action_outside_player_turn(tmp_path):
    async def run():
        memory = BuckshotMemory(tmp_path / "db.sqlite")
        match_id = memory.start_match("qq:1", "demon")
        engine = MockBuckshotGame(seed=1)
        engine.start()
        engine.turn = "dealer"

        async def noop(*args, **kwargs):
            return None

        async def ai_decide(view):
            raise AssertionError("dealer AI must not run for an out-of-turn action")

        match = MockMatch(
            match_id=match_id, player_key="qq:1", player_name="测试", memory_mode="off",
            learn=False, memory=memory, engine=engine, ai_decide=ai_decide,
            send_qq=noop, send_state=noop, send_event=noop, send_private=noop,
        )
        ok, message = await match.player_action({"action": "shoot", "target": "dealer"})
        assert ok is False
        assert "回合" in message
        assert engine.turn == "dealer"
        await match.close()
        memory.close()

    asyncio.run(run())


def test_private_peek_not_in_transcript(tmp_path):
    async def run():
        memory = BuckshotMemory(tmp_path / "db.sqlite")
        match_id = memory.start_match("qq:1", "demon")
        private = []
        async def send_state(state): pass
        async def send_event(event): pass
        async def send_private(peek): private.append(peek)
        async def send_qq(text): pass
        async def ai_decide(view):
            return {"game_action": fallback_action(view), "qq_message": "",
                    "memory_note": ""}
        engine = MockBuckshotGame(seed=3)
        match = MockMatch(
            match_id=match_id, player_key="qq:1", player_name="测试", memory_mode="off",
            learn=False, memory=memory, engine=engine, ai_decide=ai_decide,
            send_qq=send_qq, send_state=send_state, send_event=send_event,
            send_private=send_private,
        )
        await match.start()
        engine.shells = ["LIVE", "BLANK"]
        engine.player.items = ["magnifying glass", "beer"]
        ok, _ = await match.player_action({"action": "use_item", "slot_index": 0})
        assert ok
        assert private and private[0]["shell"] == "LIVE"
        assert "实弹 (LIVE)" not in "\n".join(match.transcript)
        await match.close()
        memory.close()

    asyncio.run(run())
