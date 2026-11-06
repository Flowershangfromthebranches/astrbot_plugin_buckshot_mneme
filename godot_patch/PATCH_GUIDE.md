# 《恶魔轮盘赌》AstrBotBridge 补丁指南

本目录把 `AstrBotBridge.gd` 接入反编译后的 Godot 4.1.1《恶魔轮盘赌》工程：

- 自动/手动画面推流（只在有窗口时有效；`--headless` 的 dummy viewport 没有纹理）；
- 网页动作 → 游戏内 `ItemInteraction` / `ShotgunShooting`；
- 恶魔回合 → `DealerIntelligence` 接管；
- 合法视野：`dealer_view()` 永远不包含 `sequenceArray`；
- 玩家放大镜/手机的结果走 `private_peek`，只给玩家网页，不进 AI 记忆；
- 自动跳过豁免书/开场对话，并自动完成“手提箱拿道具→放网格”阶段。

> 游戏本体是玩家自己的资产。不要把反编译源码、PCK 或资源提交到公开仓库。

## 0. 游戏桥 token

插件会给 `/ws/game` 生成随机 `bridge_token`，并在启动 Godot 时通过 `BUCKSHOT_BRIDGE_TOKEN` 环境变量传给桥脚本；`AstrBotBridge.gd` 会把它拼到 WebSocket URL 上。手动启动 Godot 或使用 `external` 模式时，需要在游戏进程设置同一个环境变量；或在插件配置里填一个固定的 `bridge_token`，再把它设进游戏进程。没有 token 的 `/ws/game` 会被 403，避免公网隧道把本地游戏接口暴露出去。

## 1. 一键打补丁

先把 PCK 中的脚本/场景/配置提取出来（完整资源可用 `--out` 不带 `--scripts-only`）：

```bash
python3 godot_patch/tools/extract_pck.py \
  "/path/to/Buckshot Roulette.x86_64" \
  --out /path/to/buckshot_src --scripts-only
```

复制成一份可修改工程，然后运行：

```bash
python3 godot_patch/apply_patch.py /path/to/buckshot_src
```

它会：

1. 修改 `scripts/DealerIntelligence.gd`（BeginDealerTurn 接管 + 两个 AstrBot 方法）；
2. 修改 `scripts/RoundManager.gd`（BeginPlayerTurn 末回调）；
3. 修改 `scripts/ShellLoader.gd`（LoadShells 末回调，第一回合也能回到网页）；
4. 修改 `scripts/MedicineManager.gd`（过期药后回到 AI 决策）；
5. 修改 `scripts/BurnerPhone.gd`（玩家手机结果只发给玩家网页）；
6. 修改 `scripts/InteractionManager.gd`（可重放 alias + 手动操作兼容点）；
7. 修改 `scripts/GlobalVariables.gd`（自动实例化 `/root/AstrBotBridge`）；
8. 修改 `scripts/Steam.gd`（动态 `Engine.get_singleton("Steam")`，方便官方 Godot 加载 PCK）；
9. 复制 `AstrBotBridge.gd` 到工程的 `godot_patch/`。

如果使用完整 Godot 工程而不是 PCK，也可以在 `project.godot` 增加 Autoload：

```ini
[autoload]
AstrBotBridge="*res://godot_patch/AstrBotBridge.gd"
```

`GlobalVariables.gd` 里有 `get_node_or_null("/root/AstrBotBridge")` 防重复，Autoload 和自动实例化可以共存。

## 2. 手动补丁锚点

### DealerIntelligence.gd

在 `BeginDealerTurn()` 第一行插入：

```gdscript
func BeginDealerTurn():
	var astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
	if astrbot_bridge != null and astrbot_bridge.ai_controlled:
		astrbot_bridge.on_dealer_turn()
		return
	mainLoopFinished = false
```

文件末尾追加：

- `AstrBotDealerUseItem(itemName)`：处理手锯、手铐、啤酒、放大镜、手机、逆转器、过期药等道具的动画与状态；
- `AstrBotDealerShoot(who)`：拿枪并调用原有 `Shoot(who)`，保留死亡/退弹/回合逻辑。

方法体见 `apply_patch.py` 的 `DEALER_METHODS`，或在打补丁后查看 `scripts/DealerIntelligence.gd` 末尾。关键点：

- 放大器：`bridge.record_dealer_glass(shellSpawner.sequenceArray[0])`；
- 手机：`bridge.choose_dealer_phone_reveal(shellSpawner.sequenceArray)`；
- 其他道具：`await bridge.after_dealer_item(itemName)` 请求下一次 AI 决策；
- 过期药：由 `MedicineManager` 的药效结束后调用 `bridge.after_dealer_medicine()`；
- 最后一发禁止使用啤酒（原版 `DealerChoice` 也会回避）。

### InteractionManager.gd

在 `"text dealer"` / `"text you"` 分支加一个兼容通知：

```gdscript
		"text dealer":
			if (!fs_dec):
				AstrBotBridgeNotify("dealer")
				shotgun.Shoot("dealer")
```

文件末尾加：

```gdscript
func AstrBotPlayerAction(alias : String):
	InteractWith(alias)

func AstrBotBridgeNotify(target : String):
	var bridge = get_node_or_null("/root/AstrBotBridge")
	if bridge != null and bridge.has_method("on_manual_player_shot"):
		bridge.on_manual_player_shot(target)
```

网页操作本身不依赖鼠标悬停：桥直接调用 `ItemInteraction.PickupItemFromTable()` 和 `ShotgunShooting.Shoot()`，这是为了能 `await` 动画并读取结果；`AstrBotPlayerAction` 是给非射击 alias 预留的入口。

### RoundManager.gd

在 `BeginPlayerTurn()` 末尾、`SetupDeskUI()` 之后：

```gdscript
	var astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
	if astrbot_bridge != null:
		astrbot_bridge.on_player_turn()
```

### ShellLoader.gd

在 `LoadShells()` 最后 `roundManager.SetupDeskUI()` 之后加同一段。没有它，第一回合桥会停留在 `waiting`（桥另有 5 秒兜底，但不建议依赖）。

### MedicineManager.gd

`UseMedicine_Dealer()` 里两处 `dealerai.DealerChoice()` 改成：

```gdscript
		var astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
		if astrbot_bridge != null and astrbot_bridge.ai_controlled:
			await astrbot_bridge.after_dealer_medicine()
		else:
			dealerai.DealerChoice()
```

### BurnerPhone.gd

在 `SendDialogue()` 选中 `randindex` 后、显示文本前：

```gdscript
		var astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
		if astrbot_bridge != null:
			astrbot_bridge.report_player_phone(randindex, sequence[randindex])
```

桥内部还会检查 `_player_phone_pending`，所以恶魔使用手机不会把结果误发给玩家。

### GlobalVariables.gd

在 `_ready()` 末尾自动创建桥（PCK 运行方式必需；完整工程用 Autoload 时此段可留作防重复）：

```gdscript
	var astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
	if astrbot_bridge == null:
		var bridge_script = load("res://godot_patch/AstrBotBridge.gd")
		if bridge_script != null:
			astrbot_bridge = bridge_script.new()
			astrbot_bridge.name = "AstrBotBridge"
			get_tree().root.add_child.call_deferred(astrbot_bridge)
```

## 3. 视野与隐私不变量

这是整个实现最重要的部分，修改代码时不要破坏：

1. `dealer_view()` 由桥显式构造，只包含 `stage / hp / 剩余实弹数 / 剩余空包数 / 双方道具 / 手锯与手铐状态 / 已验证枪膛 / 已验证未来`。
2. 永远不要把 `shellSpawner.sequenceArray`、`sequenceArray_knownShell` 或任何完整排列写进发给 Python 的 `dealer_turn_request`。
3. 玩家使用放大镜/手机时，桥发 `type="private_peek"`，Python Web 服务只转发给玩家 WebSocket，不调用插件回调，不写记忆；`BuckshotMemory.render_dealer_view()` 也不会读这些字段。
4. 恶魔自己的放大镜/手机只更新 `_ai_known_current` / `_ai_known_future`，下一条 `dealer_turn_request` 才带出。
5. 网络层还有第二道防线：`GameWebServer.broadcast_state()` 会递归移除 `sequence / shells / known_future / current_chamber_shell / player_private` 等字段，然后才发给玩家页面。

## 4. 在 macOS 上运行

如果你拿到的是 `Buckshot Roulette.x86_64`（**Linux x86_64 ELF**），它不能直接在 macOS 原生运行。三条路：

### 方式 A（macOS 已验证）：PCK + 官方 Godot 4.1.1 macOS

1. 下载 `Godot_v4.1.1-stable_macos.universal.zip`（官方 GitHub release）。
2. 打补丁并用 `repack_pck.py` 重打独立 PCK：

```bash
python3 godot_patch/tools/repack_pck.py \
  "/path/to/Buckshot Roulette.x86_64" \
  /path/to/buckshot_patched.pck \
  --replace-dir /path/to/buckshot_src
```

3. 运行：

```bash
/path/to/Godot.app/Contents/MacOS/Godot \
  --main-pack /path/to/buckshot_patched.pck --resolution 960x540
```

4. 在插件里配置：

```json
{
  "game_mode": "godot",
  "godot_executable": "/path/to/Godot.app/Contents/MacOS/Godot",
  "godot_main_pack": "/path/to/buckshot_patched.pck"
}
```

打开网页后桥会连接 `ws://127.0.0.1:19528/ws/game`，发送 `start_match`，桥会自动切到 `res://scenes/main.tscn`、跳过豁免书、自动分道具并进入赌桌。

**实测记录（2026-11-06）**：用官方 Godot 4.1.1 macOS、窗口模式、960x540 运行重打的 PCK，成功收到 960x540 JPEG 画面；桥输出 `stage=1`、`waiting → player → dealer` 状态；玩家开枪后恶魔请求决策，恶魔朝自己开空包，回合与剩余弹数同步更新（harness 记录 states=11、dealer_requests=1、frame=True）。`--headless` 的 dummy 渲染器没有 viewport 纹理，因此**不要用 headless 验证画面流**。

**Steam 提示**：官方 Godot 没有 GodotSteam 单例，`AchievementManager.gd` / `LeaderboardManager.gd` 会在控制台报 `Identifier "Steam" not declared`，但主赌桌仍能运行（这两个系统只影响成就/排行榜）。`Steam.gd` 本身已改为动态查找；若希望零报错，请使用 GodotSteam 的 macOS 编辑器/导出模板，或把这两个脚本的 `Steam.` 也改成 `Engine.get_singleton("Steam")`。

### 方式 B：使用 macOS 版《恶魔轮盘赌》

如果有 macOS 版游戏/工程，直接用 `apply_patch.py` 修改工程并导出；BepInEx 不需要，Godot 脚本补丁即可。桥的 Autoload 路径建议：

```ini
[autoload]
AstrBotBridge="*res://godot_patch/AstrBotBridge.gd"
```

### 方式 C：Linux 虚拟机 / Steam Proton / 云主机

在 Linux 环境直接运行原版可执行文件也可以，但要把桥放进去（同样的工程补丁）；macOS 上建议用方式 A。

## 5. 已知限制与排查

- **不要用 headless 测试画面**：`get_viewport().get_texture()` 在 dummy 渲染器下返回空；只测试状态时可用 headless，但画面流必须窗口模式。
- **没有状态**：确认 `AstrBotBridge` 在 `/root/AstrBotBridge`（`GlobalVariables` 补丁或 Autoload），且 19528 端口上的 Python Web 服务已启动。桥的 console 会打印 `DealerIntelligence found` / `first state`。
- **恶魔卡住**：确认 `DealerIntelligence.BeginDealerTurn` 顶部有桥接；`ai_controlled` 为 true；Python 有 `dealer_turn_request` 回调并发送了 action。
- **玩家第一回合不能操作**：检查 `ShellLoader.LoadShells` 的 `on_player_turn()` 补丁；桥有 5 秒兜底但不推荐。
- **道具阶段卡住**：桥会自动 `GrabItem` + `PlaceDownItem`；若你的工程改了 `ItemManager`，需要保留 `interaction_intake`、`gridOccupiedArray` 等字段。
- **肾上腺素**：需要二次选择偷取目标，网页 v1 不暴露；桥从两侧道具列表过滤掉，避免 AI 选到无法执行的动作。
- **PCK 重新打包后资源损坏**：确认使用本目录的 `repack_pck.py`（已修正未替换条目读取源偏移的 bug）；不要用未打补丁的旧版本工具。
- **Steam 报错**：见方式 A 的说明；不影响主赌桌。
- **不要公开分发**：PCK、反编译脚本、游戏资源受原始游戏许可约束；本仓库只提供桥、补丁和工具。

## 6. 文件

```text
godot_patch/
├── AstrBotBridge.gd          # 放到工程 res://godot_patch/ 下
├── apply_patch.py            # 自动改 8 个脚本 + 复制桥
├── PATCH_GUIDE.md
└── tools/
    ├── extract_pck.py        # 从可执行文件提取 PCK / 独立 .pck
    └── repack_pck.py         # 用补丁后的脚本重打 PCK
```
