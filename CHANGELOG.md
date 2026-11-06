# Changelog

## 0.1.2 - 2026-11-06

- 按 AstrBot 插件规范统一日志：全部使用 `from astrbot.api import logger`，移除 `import logging` 与 `logging.getLogger`。
- `GameWebServer.url_for()` 改为 async，局域网 IP 解析放入 `asyncio.to_thread` 并缓存，避免阻塞事件循环。
- 新增 url_for 异步回归测试。

## 0.1.1 - 2026-11-06

- 修复语义开局监听器优先级过低、被前置消息处理器挡掉的问题；明确说“来一把恶魔轮盘赌”会直接开局。
- 明确的开局话术不再先追问确认；疑似时仍保留人格确认。
- 私聊语义判断超时默认提高到 25 秒，避免慢模型下误放行。

## 0.1.0 - 2026-11-06

- 初版：AstrBot + NapCat 的《恶魔轮盘赌》网页控制器与人格 LLM 庄家。
- 内置 Mock 引擎，无 Godot 也能完整游玩；Mock 每关双方同血（3/3/4）。
- 三层 mneme 记忆：本局战术便条、跨局玩家画像、博弈法则；600 token 注入预算。
- 自然语言语义开局/重开：疑似时先以人格确认，明确同意后才执行；斜杠命令作为显式入口。
- 动画牌桌：开枪、掉血、用道具、装填、私有验弹；Godot 帧到达时自动切换实时画面。
- Cloudflare Tunnel 远程游玩：开局自动生成公网 HTTPS 链接，手机流量可玩。
- Godot 桥与补丁工具：合法视野、私有验弹隔离、独立 bridge token。
