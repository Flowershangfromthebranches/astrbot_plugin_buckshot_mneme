#!/usr/bin/env python3
"""无 AstrBot 的实机联调：启动 Web 控制器 + 打补丁的 Godot PCK，自动打一两回合。

示例：
  python3 integration_harness.py \
    --godot "/tmp/godot411/Godot.app/Contents/MacOS/Godot" \
    --pck /tmp/buckshot_patched.pck \
    --seconds 45 --frame-out /tmp/buckshot_frame.jpg

这不是单元测试；需要图形窗口，headless 没有 viewport 纹理。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from game.decision import fallback_action  # noqa: E402
from web_controller.server import GameWebServer  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--godot", required=True)
    parser.add_argument("--pck", required=True)
    parser.add_argument("--port", type=int, default=19528)
    parser.add_argument("--seconds", type=float, default=45)
    parser.add_argument("--frame-out", default="/tmp/buckshot_frame.jpg")
    args = parser.parse_args()

    state_count = 0
    dealer_count = 0
    game_over = asyncio.Event()
    last_player_action = 0.0
    frame_saved = False

    async def on_game_connected():
        print("[harness] game bridge connected", flush=True)
        await asyncio.sleep(0.5)
        await server.send_to_game({"type": "start_match", "match_id": "harness"})

    async def on_game_message(msg: dict):
        nonlocal state_count, dealer_count, last_player_action, frame_saved
        kind = msg.get("type")
        if kind == "state":
            state_count += 1
            state = msg.get("state") or {}
            if state_count <= 12 or state.get("turn") in ("player", "dealer"):
                print("[state]", {k: state.get(k) for k in
                                  ("stage", "turn", "dealer_hp", "player_hp",
                                   "remaining_live_count", "remaining_blank_count")}, flush=True)
            if not frame_saved and server.last_frame:
                Path(args.frame_out).write_bytes(server.last_frame)
                frame_saved = True
                print(f"[frame] saved {len(server.last_frame)} bytes -> {args.frame_out}", flush=True)
            if state.get("game_over"):
                game_over.set()
            if (state.get("turn") == "player" and not state.get("game_over")
                    and time.monotonic() - last_player_action > 7):
                last_player_action = time.monotonic()
                await asyncio.sleep(0.8)
                await server.send_to_game({"type": "action", "role": "player",
                                           "action": "shoot", "target": "dealer"})
                print("[harness] player -> shoot dealer", flush=True)
        elif kind == "dealer_turn_request":
            dealer_count += 1
            view = msg.get("view") or {}
            action = fallback_action(view)
            print("[dealer request]", view.get("remaining_live_count"),
                  view.get("remaining_blank_count"), "->", action, flush=True)
            await server.send_to_game({
                "type": "action", "role": "dealer", "action": action["type"],
                "item": action.get("item"), "target": action.get("target"),
            })
        elif kind == "event":
            print("[event]", msg.get("event"), msg.get("result", ""), flush=True)
            if msg.get("event") == "game_over":
                game_over.set()

    server = GameWebServer("127.0.0.1", args.port,
                           Path(__file__).resolve().parents[2] / "web_controller" / "static",
                           on_game_connected=on_game_connected,
                           on_game_message=on_game_message)
    await server.start()
    proc = await asyncio.create_subprocess_exec(
        args.godot, "--windowed", "--resolution", "960x540",
        "--main-pack", args.pck,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    async def read_log():
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if "AstrBotBridge" in text or "SCRIPT ERROR" in text or "ERROR:" in text:
                print("[godot]", text, flush=True)

    reader = asyncio.create_task(read_log())
    try:
        try:
            await asyncio.wait_for(game_over.wait(), timeout=args.seconds)
        except asyncio.TimeoutError:
            pass
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=8)
            except asyncio.TimeoutError:
                proc.kill()
        await asyncio.sleep(0.3)
        await server.stop()
        reader.cancel()
    print(f"[harness] states={state_count} dealer_requests={dealer_count} frame={frame_saved}",
          flush=True)


if __name__ == "__main__":
    asyncio.run(main())
