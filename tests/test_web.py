from __future__ import annotations

import asyncio
import json
import socket

import aiohttp

from web_controller.server import GameWebServer, lan_ip, pick_lan_ip


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_pick_lan_ip_skips_vpn_and_loopback():
    candidates = [
        ("lo0", "127.0.0.1"),
        ("utun4", "198.18.0.1"),
        ("en1", "192.168.2.10"),
        ("en0", "192.168.1.50"),
        ("en3", "169.254.10.20"),
    ]
    assert pick_lan_ip(candidates) == "192.168.1.50"
    assert pick_lan_ip([("utun4", "198.18.0.1"), ("lo0", "127.0.0.1")]) == ""


def test_lan_ip_is_not_vpn():
    assert not lan_ip().startswith("198.18.")


def test_url_for_is_async_and_cached():
    async def run():
        server = GameWebServer("127.0.0.1", 12345, "web_controller/static")
        server.reset_match("match-url", "token-url", "测试玩家")
        url = await server.url_for()
        assert url == "http://127.0.0.1:12345/?token=token-url"

    asyncio.run(run())


def test_web_isolation_and_relay():
    async def run():
        port = _free_port()
        player_actions, game_messages = [], []

        async def on_player_action(payload): player_actions.append(payload)
        async def on_game_message(payload): game_messages.append(payload)

        server = GameWebServer(
            "127.0.0.1", port, "web_controller/static",
            on_player_action=on_player_action, on_game_message=on_game_message,
            bridge_token="bridge-secret")
        await server.start()
        server.reset_match("match-1", "token-1", "测试玩家", dealer_name="小小鱼")
        try:
            async with aiohttp.ClientSession() as session:
                try:
                    await session.ws_connect(f"http://127.0.0.1:{port}/ws/player?token=bad")
                    raise AssertionError("invalid token should be rejected")
                except aiohttp.WSServerHandshakeError as exc:
                    assert exc.status == 403
                player = await session.ws_connect(
                    f"http://127.0.0.1:{port}/ws/player?token=token-1")
                hello = await player.receive_json()
                assert hello["type"] == "hello" and hello["match_id"] == "match-1"
                assert hello["dealer_name"] == "小小鱼"
                try:
                    await session.ws_connect(f"http://127.0.0.1:{port}/ws/game")
                    raise AssertionError("bridge token should be required")
                except aiohttp.WSServerHandshakeError as exc:
                    assert exc.status == 403
                game = await session.ws_connect(
                    f"http://127.0.0.1:{port}/ws/game?token=bridge-secret")
                assert (await game.receive_json())["type"] == "hello"

                await game.send_str(json.dumps({
                    "type": "state",
                    "state": {"stage": 1, "turn": "player",
                              "remaining_live_count": 2, "sequence": ["LIVE", "LIVE"]},
                }))
                state = await player.receive_json()
                assert state["type"] == "state"
                assert "sequence" not in state["state"]
                assert state["state"]["remaining_live_count"] == 2
                assert state["state"]["dealer_name"] == "小小鱼"

                await game.send_str(json.dumps({
                    "type": "private_peek", "info": "当前枪膛内为：实弹 (LIVE)",
                    "shell": "LIVE",
                }))
                peek = await player.receive_json()
                assert peek["type"] == "private_peek"
                assert "实弹" in peek["info"]
                assert peek["shell"] == "LIVE"

                await game.send_str(json.dumps({
                    "type": "dealer_turn_request", "view": {"remaining_live_count": 2},
                }))
                await asyncio.sleep(0.05)
                assert game_messages and game_messages[-1]["type"] == "dealer_turn_request"

                await player.send_str(json.dumps({"action": "shoot", "target": "dealer"}))
                await asyncio.sleep(0.05)
                assert player_actions and player_actions[-1]["target"] == "dealer"

                await game.send_bytes(b"jpeg-bytes")
                frame = await player.receive()
                assert frame.type == aiohttp.WSMsgType.BINARY
                assert frame.data == b"jpeg-bytes"

                async with session.get(f"http://127.0.0.1:{port}/") as response:
                    assert response.status == 200
                    assert "恶魔轮盘赌" in await response.text()
                async with session.get(f"http://127.0.0.1:{port}/health") as response:
                    body = await response.json()
                    assert body["ok"] and body["game_connected"]

                await player.close()
                await game.close()
        finally:
            await server.stop()

    asyncio.run(run())
