"""与 AstrBot 共用事件循环的本地 Web 控制器。

页面只拿公有状态；dealer_turn_request 的合法视野只交给插件/AI；
private_peek 只发给玩家 WebSocket，绝不进入 AI 上下文。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from pathlib import Path
from urllib.parse import quote

from aiohttp import WSMsgType, web

SENSITIVE_KEYS = {
    "sequence", "shell_sequence", "shells", "player_private", "private_peek",
    "known_future", "current_chamber_shell", "dealer_view", "raw_sequence",
}


def sanitize_public(payload):
    if isinstance(payload, dict):
        return {key: sanitize_public(value) for key, value in payload.items()
                if key not in SENSITIVE_KEYS}
    if isinstance(payload, list):
        return [sanitize_public(item) for item in payload]
    return payload


_VIRTUAL_IFACE_MARKERS = (
    "utun", "tun", "tap", "awdl", "llw", "anpi", "bridge", "nan", "gif", "stf",
)


def _usable_lan_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4 or addr.is_loopback or addr.is_link_local:
        return False
    if addr.is_multicast or addr.is_unspecified:
        return False
    # 198.18.0.0/15 is used by VPN/benchmark tunnels and is not reachable by phones.
    if addr in ipaddress.ip_network("198.18.0.0/15"):
        return False
    return True


def _iface_rank(name: str) -> int:
    lowered = name.lower()
    if any(marker in lowered for marker in _VIRTUAL_IFACE_MARKERS):
        return -1
    if lowered in ("en0", "wlan0", "eth0"):
        return 100
    if lowered.startswith(("en", "wlan", "eth")):
        return 50
    return 10


def _ip_rank(ip: str) -> int:
    if ip.startswith("192.168."):
        return 30
    if ip.startswith("10."):
        return 20
    if ip.startswith("172."):
        return 15
    return 0


def pick_lan_ip(candidates) -> str:
    best_ip = ""
    best_score = -1
    for name, ip in candidates:
        if not _usable_lan_ip(ip):
            continue
        score = _iface_rank(name) + _ip_rank(ip)
        if score > best_score:
            best_ip, best_score = ip, score
    return best_ip


def lan_ip() -> str:
    candidates: list[tuple[str, str]] = []
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            stats = psutil.net_if_stats()
            for name, addrs in psutil.net_if_addrs().items():
                stat = stats.get(name)
                if stat is not None and not stat.isup:
                    continue
                for addr in addrs:
                    if addr.family == socket.AF_INET:
                        candidates.append((name, addr.address))
        except Exception:
            candidates.clear()
    if not candidates:
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                candidates.append(("hostname", ip))
        except OSError:
            pass
    if not candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            candidates.append(("udp", sock.getsockname()[0]))
        except OSError:
            pass
        finally:
            sock.close()
    return pick_lan_ip(candidates) or "127.0.0.1"


class GameWebServer:
    def __init__(self, host: str, port: int, static_dir: str | Path, *,
                 on_player_action=None, on_game_message=None, on_game_connected=None,
                 public_base_url: str = "", bridge_token: str = "",
                 logger: logging.Logger | None = None):
        self.host = host
        self.port = int(port)
        self.static_dir = Path(static_dir)
        self.bridge_token = (bridge_token or "").strip()
        self.on_player_action = on_player_action
        self.on_game_message = on_game_message
        self.on_game_connected = on_game_connected
        self.public_base_url = (public_base_url or "").rstrip("/")
        self.logger = logger or logging.getLogger(__name__)
        self.token: str | None = None
        self.match_id: str | None = None
        self.player_name = "玩家"
        self.dealer_name = "庄家"
        self.last_state: dict | None = None
        self.last_frame: bytes | None = None
        self._players: set[web.WebSocketResponse] = set()
        self._game: web.WebSocketResponse | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/health", self._health)
        app.router.add_get("/ws/player", self._player_ws)
        app.router.add_get("/ws/game", self._game_ws)
        app.router.add_static("/static", self.static_dir, show_index=False)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self.logger.info("[buckshot] web controller on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        async with self._lock:
            players = list(self._players)
            self._players.clear()
            game = self._game
            self._game = None
        for ws in players:
            try:
                await ws.close(code=1001, message=b"server stopping")
            except Exception:
                pass
        if game is not None:
            try:
                await game.close(code=1001, message=b"server stopping")
            except Exception:
                pass
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    @property
    def game_connected(self) -> bool:
        return self._game is not None and not self._game.closed

    @property
    def player_connected(self) -> bool:
        return any(not ws.closed for ws in self._players)

    def reset_match(self, match_id: str, token: str, player_name: str = "玩家",
                    dealer_name: str = "庄家") -> None:
        self.match_id = match_id
        self.token = token
        self.player_name = player_name
        self.dealer_name = str(dealer_name or "庄家")
        self.last_state = None
        self.last_frame = None
        for ws in list(self._players):
            try:
                asyncio.create_task(ws.close(code=4000, message=b"new match"))
            except Exception:
                pass
        self._players.clear()

    def url_for(self) -> str:
        base = self.public_base_url
        if not base:
            host = lan_ip() if self.host in ("0.0.0.0", "::") else self.host
            base = f"http://{host}:{self.port}"
            self.logger.info("[buckshot] player link host=%s", host)
        token = quote(self.token or "")
        return f"{base}/?token={token}"

    # ------------------------------------------------------------ 基本页面

    async def _index(self, request: web.Request) -> web.StreamResponse:
        path = self.static_dir / "index.html"
        if not path.exists():
            return web.Response(text="index.html 缺失", status=500)
        return web.FileResponse(path)

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "match_id": self.match_id,
                                  "game_connected": self.game_connected,
                                  "players": len(self._players)})

    async def _player_ws(self, request: web.Request) -> web.StreamResponse:
        if not self.token or request.query.get("token") != self.token:
            return web.Response(status=403, text="invalid token")
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=2 * 1024 * 1024)
        await ws.prepare(request)
        self._players.add(ws)
        try:
            await ws.send_json({"type": "hello", "match_id": self.match_id,
                                "player_name": self.player_name,
                                "dealer_name": self.dealer_name})
            if self.last_state is not None:
                await ws.send_json({"type": "state", "state": sanitize_public(self.last_state)})
            if self.last_frame is not None:
                await ws.send_bytes(self.last_frame)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict) and self.on_player_action is not None:
                        try:
                            await self.on_player_action(payload)
                        except Exception:
                            self.logger.exception("[buckshot] player action failed")
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
            self._players.discard(ws)
        return ws

    async def _game_ws(self, request: web.Request) -> web.StreamResponse:
        if self.bridge_token:
            if request.query.get("token") != self.bridge_token:
                return web.Response(status=403, text="invalid bridge token")
        else:
            remote = request.remote or ""
            if remote not in ("127.0.0.1", "::1", "localhost"):
                return web.Response(status=403, text="game bridge must be loopback")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        async with self._lock:
            old = self._game
            self._game = ws
        if old is not None and not old.closed:
            try:
                await old.close(code=4001, message=b"replaced")
            except Exception:
                pass
        self.logger.info("[buckshot] Godot bridge connected")
        try:
            await ws.send_json({"type": "hello", "match_id": self.match_id})
            if self.on_game_connected is not None:
                try:
                    await self.on_game_connected()
                except Exception:
                    self.logger.exception("[buckshot] on_game_connected failed")
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    await self.broadcast_frame(message.data)
                elif message.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    await self._route_game_message(payload)
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
            async with self._lock:
                if self._game is ws:
                    self._game = None
            self.logger.info("[buckshot] Godot bridge disconnected")
        return ws

    async def _route_game_message(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind == "state":
            state = payload.get("state") or payload.get("public_state") or {}
            await self.broadcast_state(state)
            if self.on_game_message is not None:
                await self.on_game_message(payload)
        elif kind == "event":
            await self.broadcast_event(payload)
            if self.on_game_message is not None:
                await self.on_game_message(payload)
        elif kind == "private_peek":
            # 只给玩家；不调用插件，不写 last_state。
            await self.send_private_peek(payload)
        elif kind == "dealer_turn_request":
            # 合法视野只交给插件；玩家页面看不到。
            if self.on_game_message is not None:
                await self.on_game_message(payload)
        else:
            if self.on_game_message is not None:
                await self.on_game_message(payload)

    # ------------------------------------------------------------ 发送

    async def broadcast_state(self, state: dict) -> None:
        payload = dict(state or {})
        payload["dealer_name"] = self.dealer_name
        self.last_state = sanitize_public(payload)
        await self._broadcast_json({"type": "state", "state": self.last_state})

    async def broadcast_event(self, event: dict) -> None:
        await self._broadcast_json({"type": "event", "event": event})

    async def send_private_peek(self, peek: dict) -> None:
        payload = {"type": "private_peek", "info": peek.get("info", ""),
                   "source": peek.get("source", ""), "offset": peek.get("offset", 0),
                   "shell": peek.get("shell")}
        await self._broadcast_json(payload, players_only=True)

    async def broadcast_frame(self, jpeg: bytes) -> None:
        self.last_frame = jpeg
        for ws in list(self._players):
            if ws.closed:
                self._players.discard(ws)
                continue
            try:
                await ws.send_bytes(jpeg)
            except Exception:
                self._players.discard(ws)

    async def send_to_game(self, payload: dict) -> bool:
        game = self._game
        if game is None or game.closed:
            return False
        try:
            await game.send_json(payload)
            return True
        except Exception:
            self.logger.exception("[buckshot] send to Godot failed")
            return False

    async def _broadcast_json(self, payload: dict, *, players_only: bool = True) -> None:
        for ws in list(self._players):
            if ws.closed:
                self._players.discard(ws)
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                self._players.discard(ws)
