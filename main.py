"""AstrBot 恶魔轮盘赌 × mneme 插件。

一个进程内完成：
  - AstrBot 命令 / 人格 / QQ 主动消息；
  - 网页 WebSocket 控制器（帧中继 + 玩家操作）；
  - 内置 Mock 引擎（无 Godot 也能完整测试/练习）；
  - Godot 探针桥（AstrBotBridge.gd）；
  - SQLite FTS5 三层记忆与 600 token 注入。

自然语言记忆策略不是关键词模板：主 Agent 通过 LLM tools
buckshot_start_match / buckshot_set_memory_mode / buckshot_practice
自行理解用户意图；对局内再由人格 prompt + LLM 做单次操作/台词决策。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

from .game.decision import (
    build_banter_prompt,
    build_dealer_prompt,
    build_intent_prompt,
    build_policy_prompt,
    build_review_prompt,
    event_description,
    fallback_action,
    parse_dealer_decision,
    parse_intent,
    parse_policy,
    parse_review,
    persona_system,
    resolve_intent_action,
)
from .game.match import MockMatch
from .game.mock_engine import GameError, ITEM_LABELS, MockBuckshotGame
from .game.practice import choose_player_action
from .memory.engine import BuckshotMemory
from .web_controller.server import GameWebServer

NAME = "astrbot_plugin_buckshot_mneme"
LOG = logging.getLogger(NAME)

ALLOWED_VIEW_KEYS = {
    "stage", "turn", "dealer_hp", "dealer_max_hp", "player_hp", "player_max_hp",
    "remaining_live_count", "remaining_blank_count", "barrel_sawed_off",
    "player_handcuffed", "dealer_handcuffed", "dealer_items", "player_items",
    "current_chamber_shell", "known_future", "game_over", "result",
}


@dataclass
class ActiveMatch:
    match_id: str
    player_key: str
    player_name: str
    umo: str
    persona: dict
    persona_key: str
    memory_mode: str
    learn: bool
    kind: str
    token: str
    runner: MockMatch | None = None
    state: dict = field(default_factory=dict)
    transcript: list[str] = field(default_factory=list)
    bridge_started: bool = False
    started_at: float = field(default_factory=time.time)
    finished: bool = False
    action_pending: bool = False
    dealer_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    action_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BuckshotMnemePlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.conf = config or {}
        self.plugin_name = NAME
        self.data_dir = self._resolve_data_dir()
        self.memory_db = self._resolve_db_path()
        self.memory = BuckshotMemory(self.memory_db)
        self.bridge_token = (str(self._conf("bridge_token", "") or "").strip()
                             or secrets.token_urlsafe(24))
        self.web = GameWebServer(
            self._conf("web_host", "0.0.0.0"),
            int(self._conf("web_port", 19528)),
            Path(__file__).resolve().parent / "web_controller" / "static",
            on_player_action=self._on_player_action,
            on_game_message=self._on_game_message,
            on_game_connected=self._on_game_connected,
            public_base_url=self._conf("public_base_url", ""),
            bridge_token=self.bridge_token,
            logger=self.logger,
        )
        self._active: ActiveMatch | None = None
        self.active_player_origin: str | None = None
        self.active_sender_name = ""
        self._web_started = False
        self._web_lock = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._qq_locks: dict[str, asyncio.Lock] = {}
        self._qq_last: dict[str, float] = {}
        self._practice_task: asyncio.Task | None = None
        self._pending_start: dict[str, dict] = {}
        self._game_proc: asyncio.subprocess.Process | None = None
        self._game_log = None
        self._tunnel_proc: asyncio.subprocess.Process | None = None
        self._tunnel_task: asyncio.Task | None = None
        self._tunnel_url = ""
        LOG.info("[buckshot] memory=%s web=%s:%s", self.memory_db,
                 self._conf("web_host", "0.0.0.0"), self._conf("web_port", 19528))

    # ------------------------------------------------------------ 配置/路径

    def _conf(self, key: str, default=None):
        try:
            value = self.conf.get(key, default)
        except Exception:
            value = default
        return default if value is None or value == "" else value

    def _balanced_hp_enabled(self) -> bool:
        value = self._conf("balanced_hp", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("false", "0", "no", "off", "")

    def _resolve_data_dir(self) -> Path:
        try:
            path = Path(StarTools.get_data_dir(NAME))
        except Exception:
            path = Path(__file__).resolve().parent / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_db_path(self) -> Path:
        raw = ""
        try:
            raw = str(self.conf.get("memory_db", "") or "").strip()
        except Exception:
            raw = ""
        path = Path(raw) if raw else self.data_dir / "mneme_buckshot.db"
        if not path.is_absolute():
            path = self.data_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _game_mode(self) -> str:
        mode = str(self._conf("game_mode", "mock")).strip().lower()
        return mode if mode in ("mock", "godot", "external") else "mock"

    # ------------------------------------------------------------ 人格 / LLM

    async def _resolve_persona(self, event: AstrMessageEvent) -> dict:
        umo = event.unified_msg_origin
        conv_persona = None
        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if conv_id:
                conv = await self.context.conversation_manager.get_conversation(umo, conv_id)
                conv_persona = getattr(conv, "persona_id", None) if conv else None
        except Exception:
            conv_persona = None
        try:
            _, persona, _, _ = await self.context.persona_manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conv_persona,
                platform_name=event.get_platform_name(),
                provider_settings={},
            )
            if persona:
                return dict(persona)
        except Exception:
            LOG.exception("[buckshot] resolve persona failed")
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
            if persona:
                return dict(persona)
        except Exception:
            pass
        return {"name": "default", "prompt": "你是一个阴郁、危险、喜欢心理博弈的恶魔庄家。",
                "begin_dialogs": [], "_begin_dialogs_processed": []}

    async def _provider(self, umo: str):
        wanted = str(self._conf("llm_provider_id", "")).strip()
        try:
            if wanted:
                for provider in self.context.get_all_providers():
                    meta = provider.meta()
                    if getattr(meta, "id", "") == wanted:
                        return provider
            return await self.context.get_using_provider_async(umo)
        except Exception:
            LOG.exception("[buckshot] resolve provider failed")
            return None

    async def _llm(self, umo: str, persona: dict, system: str, user: str, *,
                   with_dialogs: bool = False, timeout: float | None = None) -> str:
        provider = await self._provider(umo)
        if provider is None:
            raise RuntimeError("AstrBot 没有可用的对话模型")
        timeout = timeout or float(self._conf("llm_timeout", 35))
        contexts = None
        if with_dialogs:
            contexts = persona.get("_begin_dialogs_processed") or None
        async def call(use_contexts):
            response = await provider.text_chat(
                prompt=user, system_prompt=system, contexts=use_contexts)
            if getattr(response, "role", "") == "err":
                raise RuntimeError(getattr(response, "completion_text", "LLM error"))
            return str(getattr(response, "completion_text", "") or "")
        try:
            return await asyncio.wait_for(call(contexts), timeout=timeout)
        except Exception:
            if contexts:
                try:
                    return await asyncio.wait_for(call(None), timeout=timeout)
                except Exception:
                    raise
            raise

    def _persona_prompt(self, persona: dict) -> str:
        return str(persona.get("prompt") or persona.get("system_prompt") or "").strip()

    @staticmethod
    def _persona_display_name(persona: dict) -> str:
        name = str(persona.get("name") or persona.get("persona_id") or "").strip()
        for prefix in ("人格工坊·", "人格工坊：", "人格工坊:"):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        return name or "庄家"

    # ------------------------------------------------------------ 开始一局

    async def _start_match_for_event(self, event: AstrMessageEvent, *,
                                     forced_mode: str | None = None,
                                     allow_replace: bool = True) -> tuple[ActiveMatch, str]:
        async with self._lifecycle:
            if self._active is not None and not self._active.finished:
                if not allow_replace:
                    raise RuntimeError("当前对局还没有结束")
                await self._abort_active("新的对局开始了")
            self._pending_start.pop(event.unified_msg_origin, None)
            await self._ensure_web()
            persona = await self._resolve_persona(event)
            persona_key = str(persona.get("name") or "default")
            sender = event.get_sender_id() or event.unified_msg_origin
            player_key = f"{event.get_platform_name()}:{sender}"
            player_name = event.get_sender_name() or "玩家"
            session_policy = self.memory.policy_get(player_key, persona_key)
            stats = self.memory.stats(player_key, persona_key)
            explicit_mode = forced_mode if forced_mode in ("off", "on", "adaptive") else None
            if not explicit_mode and session_policy and session_policy.get("mode") != "auto":
                explicit_mode = str(session_policy.get("mode") or "adaptive")
            policy = await self._decide_policy(event, persona, stats, explicit_mode,
                                               session_policy)
            mode = policy["mode"]
            learn = bool(policy["learn"])
            match_id = self.memory.start_match(player_key, persona_key, memory_mode=mode)
            token = secrets.token_urlsafe(18)
            match = ActiveMatch(
                match_id=match_id, player_key=player_key, player_name=player_name,
                umo=event.unified_msg_origin, persona=persona, persona_key=persona_key,
                memory_mode=mode, learn=learn, kind="mock", token=token,
            )
            self._active = match
            self.active_player_origin = event.unified_msg_origin
            self.active_sender_name = player_name
            self.memory.set_match_mode(match_id, mode)
            self.web.reset_match(match_id, token, player_name,
                                 dealer_name=self._persona_display_name(persona))
            game_mode = self._game_mode()
            if game_mode in ("godot", "external"):
                try:
                    await self._ensure_game_process()
                    match.kind = "godot"
                except Exception as exc:
                    LOG.exception("[buckshot] Godot start failed, fallback to mock")
                    match.transcript.append(f"[系统] Godot 启动失败，已回退 Mock：{exc}")
                    match.kind = "mock"
            if match.kind == "godot" and self.web.game_connected:
                await self.web.send_to_game({"type": "start_match", "match_id": match.match_id})
                match.bridge_started = True
            if match.kind == "mock":
                engine = MockBuckshotGame(
                    seed=None, player_name=player_name,
                    balanced_hp=self._balanced_hp_enabled())
                runner = MockMatch(
                    match_id=match_id, player_key=player_key, player_name=player_name,
                    memory_mode=mode, learn=learn, memory=self.memory, engine=engine,
                    ai_decide=lambda view: self._decide_dealer(match, view, practice=False),
                    send_qq=lambda text: self._send_qq(match, text),
                    send_state=self.web.broadcast_state,
                    send_event=self.web.broadcast_event,
                    send_private=self.web.send_private_peek,
                    on_event=lambda payload, state: self._banter(match, payload, state),
                    on_finished=lambda info: self._finish_match(match, info),
                )
                match.runner = runner
                try:
                    await runner.start()
                except Exception:
                    LOG.exception("[buckshot] mock start failed")
            url = self.web.url_for()
            opening = str(policy.get("opening_line") or "").strip()
            opening = opening.replace("{{url}}", url).replace("{url}", url)
            if url not in opening:
                opening = (opening + "\n\n" if opening else "") + f"点开这张牌桌，坐到我面前：\n{url}"
            match.transcript.append(f"[开局] 模式={mode}，学习={learn}，"
                                    f"策略原因={policy.get('reason', '')}")
            return match, opening

    async def _decide_policy(self, event: AstrMessageEvent, persona: dict, stats: dict,
                             explicit_mode: str | None, session_policy: dict | None) -> dict:
        user_text = (event.message_str or "").strip()
        system, user = build_policy_prompt(
            self._persona_prompt(persona), user_text, stats,
            persona_name=self._persona_display_name(persona))
        fallback_mode = explicit_mode or "adaptive"
        fallback_learn = bool(session_policy.get("learn", True)) if session_policy else True
        result = {"mode": fallback_mode, "learn": fallback_learn,
                  "reason": "fallback", "opening_line": ""}
        try:
            text = await self._llm(event.unified_msg_origin, persona, system, user,
                                   with_dialogs=True)
            parsed = parse_policy(text)
            result.update(parsed)
        except Exception as exc:
            LOG.warning("[buckshot] policy llm failed: %s", exc)
        if explicit_mode:
            result["mode"] = explicit_mode
            if session_policy and "learn" in session_policy:
                result["learn"] = bool(session_policy.get("learn"))
        if result["mode"] == "auto":
            result["mode"] = "adaptive"
        if not result.get("opening_line"):
            opening = "赌桌已经摆好。今晚的规则很简单：一人一枪，直到有人倒下。"
            if persona.get("name"):
                opening = f"{persona['name']}已经把赌桌摆好。今晚的规则很简单：一人一枪，直到有人倒下。"
            result["opening_line"] = opening
        return result

    async def _ensure_web(self) -> None:
        async with self._web_lock:
            if self._web_started:
                await self._ensure_tunnel()
                return
            await self.web.start()
            self._web_started = True
            await self._ensure_tunnel()

    def _remote_mode(self) -> str:
        mode = str(self._conf("remote_mode", "off")).strip().lower()
        return mode if mode in ("off", "cloudflared") else "off"

    def _cloudflared_exe(self) -> str:
        configured = str(self._conf("cloudflared_path", "") or "").strip()
        if configured:
            return configured
        found = shutil.which("cloudflared")
        if found:
            return found
        for candidate in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"):
            if Path(candidate).exists():
                return candidate
        return ""

    async def _drain_tunnel_stderr(self) -> None:
        proc = self._tunnel_proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                LOG.debug("[buckshot] cloudflared: %s",
                          line.decode(errors="replace").strip())
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _ensure_tunnel(self) -> None:
        if self._remote_mode() != "cloudflared" or self.web.public_base_url:
            return
        if (self._tunnel_proc is not None and self._tunnel_proc.returncode is None
                and self._tunnel_url):
            return
        exe = self._cloudflared_exe()
        if not exe:
            LOG.warning("[buckshot] cloudflared not found; player link stays on LAN")
            return
        target_host = "127.0.0.1" if self.web.host in ("0.0.0.0", "::") else self.web.host
        target = f"http://{target_host}:{self.web.port}"
        cmd = [exe, "tunnel", "--url", target, "--no-autoupdate"]
        try:
            self._tunnel_proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
        except Exception:
            LOG.exception("[buckshot] cloudflared start failed; player link stays on LAN")
            return
        url = ""
        deadline = time.monotonic() + float(self._conf("tunnel_timeout", 20))
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                line = await asyncio.wait_for(
                    self._tunnel_proc.stderr.readline(), timeout=remaining)
                if not line:
                    break
                match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com",
                                  line.decode(errors="replace"))
                if match:
                    url = match.group(0).rstrip("/")
                    break
        except asyncio.TimeoutError:
            pass
        except Exception:
            LOG.exception("[buckshot] cloudflared URL parse failed")
        if not url:
            LOG.warning("[buckshot] cloudflared did not report a public URL")
            try:
                self._tunnel_proc.terminate()
            except Exception:
                pass
            self._tunnel_proc = None
            return
        self._tunnel_url = url
        self.web.public_base_url = url
        self._tunnel_task = asyncio.create_task(self._drain_tunnel_stderr())
        LOG.info("[buckshot] cloudflared public url=%s", url)

    async def _stop_tunnel(self) -> None:
        proc = self._tunnel_proc
        self._tunnel_proc = None
        if self._tunnel_task is not None:
            self._tunnel_task.cancel()
            self._tunnel_task = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._tunnel_url = ""

    async def _ensure_game_process(self) -> None:
        mode = self._game_mode()
        if mode == "external":
            return
        if self._game_proc is not None and self._game_proc.returncode is None:
            return
        executable = str(self._conf("godot_executable", "")).strip()
        if not executable:
            raise RuntimeError("未配置 godot_executable，无法启动 Godot 探针")
        if not Path(executable).exists():
            raise RuntimeError(f"Godot 可执行文件不存在：{executable}")
        cmd = [executable]
        project = str(self._conf("godot_project_path", "")).strip()
        main_pack = str(self._conf("godot_main_pack", "")).strip()
        if main_pack:
            cmd += ["--main-pack", main_pack]
        elif project:
            cmd += ["--path", project]
        log_path = self.data_dir / "godot_probe.log"
        self._game_log = open(log_path, "ab")
        env = os.environ.copy()
        env["BUCKSHOT_BRIDGE_TOKEN"] = self.bridge_token
        self._game_proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=project or str(Path(executable).parent),
            stdout=self._game_log, stderr=self._game_log, env=env)
        await asyncio.sleep(1.5)
        if self._game_proc.returncode is not None:
            raise RuntimeError(f"Godot 进程启动后立即退出（{self._game_proc.returncode}），"
                               f"日志：{log_path}")
        LOG.info("[buckshot] Godot started pid=%s", self._game_proc.pid)

    async def _on_game_connected(self) -> None:
        match = self._active
        if (match is not None and match.kind == "godot" and not match.finished
                and not match.bridge_started):
            await self.web.send_to_game({"type": "start_match", "match_id": match.match_id})
            match.bridge_started = True

    # ------------------------------------------------------------ 恶魔决策

    async def _decide_dealer(self, match, view: dict, *, practice: bool = False) -> dict:
        safe_view = self._sanitize_view(view)
        view_text = BuckshotMemory.render_dealer_view(safe_view)
        query = view_text + " " + " ".join(
            f"{item.get('item', '')} {item.get('label', '')}"
            for item in safe_view.get("dealer_items") or [])
        memory = self.memory.build_context(
            match.match_id, match.player_key, match.persona_key, query,
            mode=match.memory_mode, token_budget=600)
        persona_name = self._persona_display_name(match.persona)
        system = persona_system(self._persona_prompt(match.persona), persona_name)
        user = build_dealer_prompt(view_text, memory["text"], match.memory_mode,
                                   persona_name=persona_name)
        try:
            text = await self._llm(match.umo, match.persona, system, user,
                                   with_dialogs=False,
                                   timeout=float(self._conf("dealer_llm_timeout", 35)))
            decision = parse_dealer_decision(text, safe_view)
        except Exception as exc:
            LOG.warning("[buckshot] dealer llm failed: %s", exc)
            decision = parse_dealer_decision("", safe_view)
            decision["fallback"] = True
        if practice:
            decision["qq_message"] = ""
        return decision

    @staticmethod
    def _sanitize_view(view: dict) -> dict:
        out = {}
        for key in ALLOWED_VIEW_KEYS:
            if key in view:
                out[key] = view[key]
        for key in ("dealer_items", "player_items"):
            items = []
            for item in out.get(key) or []:
                if isinstance(item, dict):
                    items.append({k: item.get(k) for k in ("slot", "item", "label", "description")
                                  if k in item})
                elif isinstance(item, str):
                    items.append({"slot": len(items), "item": item,
                                  "label": ITEM_LABELS.get(item, item)})
            out[key] = items
        future = []
        for item in out.get("known_future") or []:
            if isinstance(item, dict) and item.get("shell"):
                try:
                    future.append({"offset": int(item.get("offset", 0)),
                                   "shell": str(item["shell"]).upper()})
                except (TypeError, ValueError):
                    continue
        out["known_future"] = future
        if out.get("current_chamber_shell"):
            out["current_chamber_shell"] = str(out["current_chamber_shell"]).upper()
        for key in ("remaining_live_count", "remaining_blank_count", "dealer_hp",
                    "player_hp", "dealer_max_hp", "player_max_hp", "stage"):
            try:
                out[key] = int(out.get(key, 0))
            except (TypeError, ValueError):
                out[key] = 0
        return out

    def _note_kind(self, note: str) -> str:
        return "shell" if re.search(r"实弹|空包|枪膛|第\s*\d+\s*发|手机|放大镜", note) \
            else "observation"

    # ------------------------------------------------------------ Web 回调

    async def _on_player_action(self, payload: dict) -> None:
        match = self._active
        if match is None or match.finished:
            return
        if match.kind == "godot":
            ok = await self.web.send_to_game({
                "type": "action", "role": "player",
                "action": payload.get("action"),
                "slot_index": payload.get("slot_index"),
                "target": payload.get("target"),
            })
            if not ok:
                await self.web.broadcast_event({"event": "error", "message": "游戏探针未连接"})
            return
        runner = match.runner
        if runner is not None:
            engine = runner.engine
            dealer_name = self._persona_display_name(match.persona)
            if engine.game_over:
                await self.web.broadcast_event({"event": "error", "message": "本局已经结束"})
                await self.web.broadcast_state(engine.public_state())
                return
            if engine.turn != "player":
                await self.web.broadcast_event(
                    {"event": "error", "message": f"现在是{dealer_name}的回合"})
                await self.web.broadcast_state(engine.public_state())
                return
            if match.action_pending or match.action_lock.locked():
                await self.web.broadcast_event(
                    {"event": "error", "message": "上一手还在处理，稍等"})
                await self.web.broadcast_state(engine.public_state())
                return
            match.action_pending = True
        task = asyncio.create_task(self._run_mock_action(match, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_mock_action(self, match: ActiveMatch, payload: dict) -> None:
        try:
            async with match.action_lock:
                if match.runner is None or match.finished:
                    return
                ok, message = await match.runner.player_action(payload)
                if not ok:
                    await self.web.broadcast_event({"event": "error", "message": message})
                    await self.web.broadcast_state(match.runner.engine.public_state())
        finally:
            match.action_pending = False

    async def _on_game_message(self, payload: dict) -> None:
        match = self._active
        if match is None or match.kind != "godot" or match.finished:
            return
        kind = payload.get("type")
        if kind == "state":
            state = payload.get("state") or payload.get("public_state") or {}
            match.state = self._sanitize_view(state)
            if state.get("game_over"):
                asyncio.create_task(self._finish_bridge(match, state))
            return
        if kind == "dealer_turn_request":
            view = payload.get("view") or match.state or {}
            task = asyncio.create_task(self._bridge_dealer_turn(match, view))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        if kind == "event":
            name = payload.get("event") or ""
            state = payload.get("state") or match.state
            if name == "new_loadout":
                self.memory.new_loadout(match.match_id)
                match.transcript.append("[装填] 新一轮弹仓")
            elif name in ("game_over",):
                task = asyncio.create_task(self._finish_bridge(match, payload))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            elif name.startswith(("player_shot", "player_used", "round_win", "round_lose")):
                match.transcript.append("[事件] " + event_description(name, payload))
                task = asyncio.create_task(self._banter(match, payload, state))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            elif name.startswith(("dealer_shot", "dealer_used")):
                match.transcript.append("[事件] " + event_description(name, payload))
            return
        if kind == "game_over":
            task = asyncio.create_task(self._finish_bridge(match, payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _bridge_dealer_turn(self, match: ActiveMatch, view: dict) -> None:
        async with match.dealer_lock:
            if match.finished:
                return
            safe_view = self._sanitize_view(view)
            decision = await self._decide_dealer(match, safe_view)
            message = str(decision.get("qq_message") or "").strip()
            if message:
                await self._send_qq(match, message)
            note = str(decision.get("memory_note") or "").strip()
            if note:
                self.memory.add_tactical(match.match_id, note, kind=self._note_kind(note),
                                         player_id=match.player_key, confidence="verified")
            action = decision.get("game_action") or fallback_action(safe_view)
            match.transcript.append(
                f"[恶魔] {action.get('type')} {action.get('item') or action.get('target')}"
                + (f" | {message}" if message else ""))
            payload = {"type": "action", "role": "dealer", "action": action.get("type")}
            if action.get("type") == "use_item":
                payload["item"] = action.get("item")
            else:
                payload["target"] = action.get("target")
            ok = await self.web.send_to_game(payload)
            if not ok:
                await self._send_qq(match, "赌桌探针断开了……先别急，我正在重新连上。")

    async def _finish_bridge(self, match: ActiveMatch, payload: dict) -> None:
        if match.finished:
            return
        state = payload.get("state") or match.state or {}
        result = payload.get("result") or state.get("result") or "draw"
        await self._finish_match(match, {"result": result,
                                         "transcript": "\n".join(match.transcript[-100:])})

    # ------------------------------------------------------------ QQ / 吐槽 / 复盘

    async def _send_qq(self, match: ActiveMatch, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        lock = self._qq_locks.setdefault(match.match_id, asyncio.Lock())
        async with lock:
            last = self._qq_last.get(match.match_id, 0.0)
            wait = float(self._conf("qq_min_interval", 1.2)) - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await self.context.send_message(match.umo, MessageChain([Plain(text)]))
                self._qq_last[match.match_id] = time.monotonic()
            except Exception:
                LOG.exception("[buckshot] send_message failed")

    async def _banter(self, match: ActiveMatch, payload: dict, state: dict) -> None:
        if match.finished:
            return
        name = payload.get("event") or payload.get("type") or ""
        if name == "player_used_item":
            item = str(payload.get("item") or "")
            if item == "handcuffs":
                name = "player_used_handcuffs"
            elif item == "handsaw":
                name = "player_used_handsaw"
        if name in ("new_loadout", "state", "dealer_turn"):
            return
        query = event_description(name, payload) + " " + str(state.get("last_event") or "")
        memory_text = ""
        if match.memory_mode != "off":
            ctx = self.memory.build_context(match.match_id, match.player_key, match.persona_key,
                                            query, mode=match.memory_mode, token_budget=360)
            memory_text = ctx["text"]
        system, user = build_banter_prompt(
            self._persona_prompt(match.persona), name, state, memory_text,
            match.memory_mode, persona_name=self._persona_display_name(match.persona))
        try:
            text = await self._llm(match.umo, match.persona, system, user,
                                   with_dialogs=True,
                                   timeout=float(self._conf("banter_llm_timeout", 25)))
            text = text.strip().strip('"“”')
            if text:
                await self._send_qq(match, text[:500])
        except Exception as exc:
            LOG.warning("[buckshot] banter failed: %s", exc)

    async def _finish_match(self, match: ActiveMatch, info: dict) -> None:
        if match.finished:
            return
        match.finished = True
        result = str(info.get("result") or "draw")
        transcript = str(info.get("transcript") or "\n".join(match.transcript))
        match.transcript.append(f"[结算] {result}")
        review_message = ""
        if match.learn and match.memory_mode != "off":
            try:
                system, user = build_review_prompt(
                    transcript, result,
                    persona_name=self._persona_display_name(match.persona))
                system = self._persona_prompt(match.persona) + "\n\n" + system
                text = await self._llm(match.umo, match.persona, system, user,
                                       with_dialogs=False,
                                       timeout=float(self._conf("review_llm_timeout", 45)))
                reviewed = parse_review(text)
                self.memory.commit_review(
                    match.player_key, match.match_id,
                    profile=reviewed["profile"], heuristics=reviewed["heuristics"])
                review_message = reviewed.get("qq_message", "")
            except Exception:
                LOG.exception("[buckshot] review failed")
        self.memory.end_match(match.match_id, result=result if result in
                              ("win", "loss", "draw") else "draw",
                              meta={"transcript": transcript[-8000:],
                                    "mode": match.memory_mode, "learn": match.learn})
        if review_message:
            await self._send_qq(match, review_message)
        if match.runner is not None:
            try:
                await match.runner.close()
            except Exception:
                pass

    async def _abort_active(self, reason: str) -> None:
        match = self._active
        if match is None:
            return
        match.finished = True
        if match.runner is not None:
            try:
                await match.runner.close()
            except Exception:
                pass
        try:
            self.memory.end_match(match.match_id, result="aborted",
                                  meta={"reason": reason})
        except Exception:
            pass
        self._active = None
        LOG.info("[buckshot] aborted match %s: %s", match.match_id, reason)

    # ------------------------------------------------------------ 自主练习

    async def _run_practice(self, event: AstrMessageEvent, rounds: int, focus: str) -> None:
        try:
            persona = await self._resolve_persona(event)
            persona_key = str(persona.get("name") or "default")
            uma = event.unified_msg_origin
            practice_key = f"practice:{persona_key}"
            transcripts: list[str] = []
            wins = losses = 0
            last_practice_id: str | None = None
            for _ in range(rounds):
                engine = MockBuckshotGame(
                    seed=secrets.randbits(63), player_name="练习对手",
                    balanced_hp=self._balanced_hp_enabled())
                practice_id = self.memory.start_match(practice_key, persona_key, memory_mode="on")
                last_practice_id = practice_id
                ctx = SimpleNamespace(match_id=practice_id, player_key=practice_key,
                                      persona=persona, persona_key=persona_key,
                                      memory_mode="on", umo=uma)
                events = engine.start()
                self._practice_events(events, practice_id)
                turns = 0
                while not engine.game_over and turns < 80:
                    turns += 1
                    if engine.turn == "dealer":
                        decision = await self._decide_dealer(ctx, engine.dealer_view(), practice=True)
                        note = str(decision.get("memory_note") or "").strip()
                        if note:
                            self.memory.add_tactical(practice_id, note,
                                                     kind=self._note_kind(note),
                                                     player_id=practice_key,
                                                     confidence="verified")
                        try:
                            events = engine.dealer_action(decision["game_action"])
                        except GameError:
                            events = engine.dealer_action(fallback_action(engine.dealer_view()))
                    else:
                        events = engine.player_action(choose_player_action(engine.player_view()))
                    self._practice_events(events, practice_id)
                self.memory.end_match(practice_id,
                                      result=engine.result if engine.result in
                                      ("win", "loss", "draw") else "draw")
                if engine.result == "win":
                    losses += 1  # 玩家赢 = 恶魔输
                elif engine.result == "loss":
                    wins += 1
                transcripts.append(f"练习局 {len(transcripts) + 1}: "
                                   f"结果={engine.result}，回合={turns}，"
                                   f"阶段={engine.stage}\n" + "\n".join(engine.log[-30:]))
            combined = "\n\n".join(transcripts)[-16000:]
            review_message = ""
            heuristics = []
            try:
                system, user = build_review_prompt(
                    combined, f"练习 {rounds} 局，庄家胜 {wins} 负 {losses}",
                    persona_name=self._persona_display_name(persona))
                system = self._persona_prompt(persona) + "\n\n" + system
                text = await self._llm(uma, persona, system, user,
                                       with_dialogs=False,
                                       timeout=float(self._conf("review_llm_timeout", 45)))
                reviewed = parse_review(text)
                heuristics = reviewed["heuristics"]
                review_message = reviewed.get("qq_message", "")
                if heuristics:
                    # 练习只沉淀博弈法则，不把模拟对手写进真实玩家画像。
                    self.memory.commit_review(practice_key, last_practice_id or "practice",
                                              profile=[], heuristics=heuristics)
            except Exception:
                LOG.exception("[buckshot] practice review failed")
            summary = review_message or (
                f"我练了 {rounds} 局。恶魔胜 {wins} 局，负 {losses} 局。"
                + ("记住的感觉已经写下来了。" if heuristics else "这次没总结出什么新东西。"))
            if focus:
                summary += f"\n（重点：{focus[:80]}）"
            await self.context.send_message(uma, MessageChain([Plain(summary)]))
            LOG.info("[buckshot] practice finished rounds=%s wins=%s losses=%s",
                     rounds, wins, losses)
        except Exception:
            LOG.exception("[buckshot] practice failed")
            try:
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([Plain("这次自主练习断掉了，下次再试。")]))
            except Exception:
                pass
        finally:
            self._practice_task = None

    def _practice_events(self, events: list[dict], practice_id: str) -> None:
        for payload in events:
            name = payload.get("event")
            if name == "new_loadout":
                self.memory.new_loadout(practice_id)
            # private_peek 属于模拟玩家，不进入 AI 记忆。

    # ------------------------------------------------------------ AstrBot 命令

    @filter.command("恶魔轮盘赌")
    async def start_buckshot(self, event: AstrMessageEvent):
        """显式开局命令（私聊里也支持自然语言，由人格语义判断）。"""
        try:
            match, opening = await self._start_match_for_event(event, allow_replace=True)
        except Exception as exc:
            yield event.plain_result(f"赌桌没摆起来：{exc}")
            event.stop_event()
            return
        yield event.plain_result(opening)
        event.stop_event()

    @filter.command("重开", alias={"重开一局"})
    async def restart_buckshot(self, event: AstrMessageEvent):
        """显式重开命令：替换当前对局并发送新的牌桌链接。"""
        try:
            match, opening = await self._start_match_for_event(
                event, allow_replace=True)
        except Exception as exc:
            yield event.plain_result(f"重开失败：{exc}")
            event.stop_event()
            return
        yield event.plain_result(opening)
        event.stop_event()

    def _semantic_start_enabled(self) -> bool:
        value = self._conf("semantic_start", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("false", "0", "no", "off", "")

    def _confirm_question(self, persona: dict) -> str:
        return "要我现在摆桌，陪你玩一局吗？"

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=-100)
    async def semantic_start_gate(self, event: AstrMessageEvent):
        """Private-chat semantic start gate: ask before starting, never keyword-match."""
        if event.is_stopped() or not self._semantic_start_enabled():
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith(("/", "／")):
            return
        umo = event.unified_msg_origin
        pending = self._pending_start.get(umo)
        if pending and pending.get("expires_at", 0) < time.monotonic():
            self._pending_start.pop(umo, None)
            pending = None
        active = self._active is not None and not self._active.finished
        LOG.debug("[buckshot] semantic gate active=%s", active)
        persona = await self._resolve_persona(event)
        persona_name = self._persona_display_name(persona)
        system, user = build_intent_prompt(
            self._persona_prompt(persona), persona_name, text,
            active_match=active,
            pending_question=str(pending.get("question") or "") if pending else "")
        try:
            raw = await self._llm(
                umo, persona, system, user, with_dialogs=False,
                timeout=float(self._conf("intent_llm_timeout", 10)))
            result = parse_intent(raw)
        except Exception as exc:
            LOG.warning("[buckshot] semantic start gate skipped: %s", exc)
            return
        action = resolve_intent_action(
            result["intent"], result["confidence"], has_pending=bool(pending),
            active_match=active)
        if (pending and result["intent"] in ("start", "restart")
                and result["confidence"] >= 0.5):
            action = "start_now"
        LOG.info("[buckshot] semantic start intent=%s conf=%.2f action=%s",
                 result["intent"], result["confidence"], action)
        if action == "start_now":
            self._pending_start.pop(umo, None)
            try:
                match, opening = await self._start_match_for_event(
                    event, allow_replace=True)
            except Exception as exc:
                yield event.plain_result(f"赌桌没摆起来：{exc}")
                event.stop_event()
                return
            yield event.plain_result(opening)
            event.stop_event()
            return
        if action == "ask":
            question = result.get("reply") or (
                "这局还没结束。要重开吗？" if active else self._confirm_question(persona))
            self._pending_start[umo] = {
                "question": question,
                "expires_at": time.monotonic() + 300,
                "persona": persona_name,
                "kind": "restart" if active else "start",
            }
            yield event.plain_result(question)
            event.stop_event()
            return
        if action == "decline":
            self._pending_start.pop(umo, None)
            if result.get("reply"):
                yield event.plain_result(result["reply"])
                event.stop_event()
            return

    # ------------------------------------------------------------ LLM 工具

    @filter.llm_tool(name="buckshot_start_match")
    async def buckshot_start_match_tool(self, event: AstrMessageEvent,
                                        mode: str = "", note: str = "") -> str:
        """Start a Buckshot Roulette match after the user has clearly confirmed they want to play now. If a match is already running and the user clearly asks to restart/replace it, call this to replace the old match; if the user is only ambiguous, ask a persona confirmation question first and do not call this tool yet.

        Args:
            mode(string): Optional memory mode for this match: auto, off, on, or adaptive. Leave empty to let the persona decide from context.
            note(string): Optional short note about the user's intent, for the persona's opening line.
        """
        forced = mode.strip().lower() if mode else ""
        if forced not in ("", "auto", "off", "on", "adaptive"):
            return json.dumps({"ok": False, "error": "mode 必须是 auto/off/on/adaptive"},
                              ensure_ascii=False)
        try:
            match, opening = await self._start_match_for_event(
                event, forced_mode=forced or None, allow_replace=True)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        if note:
            opening = opening + f"\n（{note[:80]}）"
        await self._send_qq(match, opening)
        return json.dumps({"ok": True, "url": self.web.url_for(), "mode": match.memory_mode,
                           "message": "对局已开始，链接和开场白已经主动发送到当前会话。"},
                          ensure_ascii=False)

    @filter.llm_tool(name="buckshot_set_memory_mode")
    async def buckshot_set_memory_mode_tool(self, event: AstrMessageEvent,
                                            mode: str = "auto",
                                            learn: bool = True,
                                            reason: str = "") -> str:
        """Adjust whether future Buckshot matches use the player's cross-match profile and learned demon heuristics. Use this when the user expresses an intent such as wanting a fair/no-memory match, wanting the demon to go easier or harder, praising or complaining about difficulty, or asking it to remember/forget its training. Do not match keywords mechanically; decide from the user's meaning and persona.

        Args:
            mode(string): auto, off, on, or adaptive. off disables cross-match memory, on uses it, adaptive lets the persona choose, auto lets this plugin decide at match start.
            learn(boolean): Whether completed matches may still update the long-term player profile and heuristics.
            reason(string): Short explanation of why this mode fits the user's intent.
        """
        mode = (mode or "auto").strip().lower()
        if mode not in ("auto", "off", "on", "adaptive"):
            return json.dumps({"ok": False, "error": "mode 必须是 auto/off/on/adaptive"},
                              ensure_ascii=False)
        persona = await self._resolve_persona(event)
        persona_key = str(persona.get("name") or "default")
        sender = event.get_sender_id() or event.unified_msg_origin
        player_key = f"{event.get_platform_name()}:{sender}"
        self.memory.policy_set(player_key, persona_key, mode=mode, learn=learn,
                               reason=(reason or "")[:200])
        match = self._active
        if match is not None and not match.finished:
            match.memory_mode = "adaptive" if mode == "auto" else mode
            match.learn = bool(learn)
            self.memory.set_match_mode(match.match_id, match.memory_mode)
            return json.dumps({"ok": True, "applied_to_active_match": True,
                               "mode": match.memory_mode, "learn": match.learn},
                              ensure_ascii=False)
        return json.dumps({"ok": True, "applied_to_active_match": False,
                           "mode": mode, "learn": bool(learn)}, ensure_ascii=False)

    @filter.llm_tool(name="buckshot_practice")
    async def buckshot_practice_tool(self, event: AstrMessageEvent,
                                     rounds: int = 2, focus: str = "") -> str:
        """Arrange a private self-practice session against simulated opponents, then improve the demon's strategic heuristics for future real matches. Use this when the user asks the demon to practice on its own, get better, or stop being weak. It runs in the background; do not claim results until a later message arrives.

        Args:
            rounds(number): Number of simulated matches to play, 1 to 3.
            focus(string): Optional aspect to focus on, such as opening shots, handcuffs, or item economy.
        """
        if self._practice_task is not None and not self._practice_task.done():
            return json.dumps({"ok": False, "error": "已经在练习了"}, ensure_ascii=False)
        if self._active is not None and not self._active.finished:
            return json.dumps({"ok": False, "error": "当前有一局正在进行，先打完再练"},
                              ensure_ascii=False)
        try:
            rounds = max(1, min(3, int(rounds)))
        except (TypeError, ValueError):
            rounds = 2
        self._practice_task = asyncio.create_task(
            self._run_practice(event, rounds, (focus or "").strip()))
        self._tasks.add(self._practice_task)
        self._practice_task.add_done_callback(self._tasks.discard)
        return json.dumps({"ok": True, "rounds": rounds,
                           "message": f"开始自主练习 {rounds} 局；练完我会主动告诉你。"},
                          ensure_ascii=False)

    # ------------------------------------------------------------ 生命周期

    async def terminate(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self._active is not None:
            try:
                await self._abort_active("插件停止")
            except Exception:
                pass
        try:
            await self._stop_tunnel()
        except Exception:
            pass
        try:
            await self.web.stop()
        except Exception:
            pass
        if self._game_proc is not None and self._game_proc.returncode is None:
            try:
                self._game_proc.terminate()
                await asyncio.sleep(0.5)
                if self._game_proc.returncode is None:
                    self._game_proc.kill()
            except Exception:
                pass
        if self._game_log is not None:
            try:
                self._game_log.close()
            except Exception:
                pass
        try:
            self.memory.close()
        except Exception:
            pass
