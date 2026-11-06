extends Node
## AstrBotBridge：Godot 4 Autoload 单例。
##
## 连接 ws://127.0.0.1:19528/ws/game（若设置了 BUCKSHOT_BRIDGE_TOKEN 则附带 token），提供：
##   1. 10 FPS / 动画期 20 FPS 的 JPEG 画面流；
##   2. 公有状态 → 玩家网页；合法视野 → 仅 AI；
##   3. 网页动作 → InteractionManager / ShotgunShooting / ItemInteraction；
##   4. DealerIntelligence 接管入口（BeginDealerTurn 补丁调用 on_dealer_turn）。
##
## 重要：dealer_view() 永不包含 sequenceArray；玩家私有验弹只走 private_peek，
## 不写入 public_state，也不会被发给 AI。

const SERVER_URL := "ws://127.0.0.1:19528/ws/game"
const MAIN_SCENE := "res://scenes/main.tscn"

const ITEM_LABELS := {
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
const ITEM_DESCRIPTIONS := {
	"handsaw": "下一枪伤害 +1",
	"magnifying glass": "查看当前枪膛",
	"beer": "退出当前子弹",
	"cigarettes": "恢复 1 点生命",
	"handcuffs": "跳过对方下一回合",
	"burner phone": "得知未来某一发",
	"expired medicine": "50% 回 2 / 50% 扣 1",
	"inverter": "翻转当前子弹",
	"adrenaline": "偷取对手一件道具",
}

var ai_controlled := false
var server_url := SERVER_URL

var _ws: WebSocketPeer
var _connected := false
var _retry_at := 0.0
var _frame_timer := 0.0
var _state_timer := 0.0
var _last_state_json := ""

var _scene: Node
var _dealer: Node
var _round_manager: Node
var _item_manager: Node
var _item_interaction: Node
var _shotgun: Node
var _shell_spawner: Node
var _intro: Node
var _menu: Node

var _turn := "player"
var _game_over := false
var _result := ""
var _last_event := ""
var _starting := false
var _game_started := false
var _player_holds_gun := false
var _pending_player_item := ""
var _player_phone_pending := false
var _auto_items_busy := false
var _auto_items_done := false
var _request_inflight := false
var _queued_dealer_request := false
var _dealer_wait_started := 0
var _waiting_since := 0

var _ai_known_current := ""
var _ai_known_future: Dictionary = {}

var _last_stage := -1
var _last_round := -1
var _last_count := -1
var _round_started := false
var _dealer_dead_reported := false
var _player_dead_reported := false
var _dealer_error_count := 0


func _ready() -> void:
	server_url = _bridge_url()
	_connect_socket()


func _bridge_url() -> String:
	var token := OS.get_environment("BUCKSHOT_BRIDGE_TOKEN")
	if token == "":
		return SERVER_URL
	return SERVER_URL + "?token=" + token.uri_encode()


func _process(delta: float) -> void:
	_poll_socket()
	_ensure_nodes()
	_stream_frame(delta)
	_poll_state(delta)
	_poll_offline_fallback()


# ---------------------------------------------------------------- WebSocket

func _connect_socket() -> void:
	_ws = WebSocketPeer.new()
	var error := _ws.connect_to_url(server_url)
	if error != OK:
		_schedule_reconnect(2.0)


func _schedule_reconnect(seconds: float) -> void:
	_retry_at = Time.get_ticks_msec() / 1000.0 + seconds


func _poll_socket() -> void:
	if _ws == null:
		if Time.get_ticks_msec() / 1000.0 >= _retry_at:
			_connect_socket()
		return
	_ws.poll()
	var state := _ws.get_ready_state()
	if state == WebSocketPeer.STATE_OPEN:
		if not _connected:
			_connected = true
			_send_json({"type": "hello", "version": "0.1.0", "scene": _scene_name()})
		while _ws.get_available_packet_count() > 0:
			var packet := _ws.get_packet()
			if _ws.was_string_packet():
				var parsed = JSON.parse_string(packet.get_string_from_utf8())
				if parsed is Dictionary:
					_handle_message(parsed)
	elif state == WebSocketPeer.STATE_CLOSED:
		if _connected:
			_connected = false
			_request_inflight = false
		if Time.get_ticks_msec() / 1000.0 >= _retry_at:
			_connect_socket()


func _send_json(payload: Dictionary) -> void:
	if _ws == null or _ws.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	_ws.send_text(JSON.stringify(payload))


func _send_bytes(data: PackedByteArray) -> void:
	if _ws == null or _ws.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	_ws.send(data, WebSocketPeer.WRITE_MODE_BINARY)


func _handle_message(payload: Dictionary) -> void:
	var kind := str(payload.get("type", ""))
	match kind:
		"start_match":
			_handle_start_match(payload)
		"action":
			# 不 await：动作里的动画协程自行推进，WebSocket 收包保持响应。
			_handle_action(payload)
		"ping":
			_send_json({"type": "pong"})


# ---------------------------------------------------------------- 场景节点

func _is_script(node: Node, name: String) -> bool:
	if node == null:
		return false
	var script = node.get_script()
	if script == null:
		return false
	return str(script.resource_path).get_file().get_basename() == name


func _find_script_node(root: Node, script_name: String) -> Node:
	if root == null:
		return null
	if _is_script(root, script_name):
		return root
	for child in root.get_children():
		var found := _find_script_node(child, script_name)
		if found != null:
			return found
	return null


func _scene_name() -> String:
	if _scene == null:
		return ""
	return str(_scene.name)


func _ensure_nodes() -> void:
	var scene := get_tree().current_scene
	if scene == null:
		return
	if scene != _scene:
		_scene = scene
		_dealer = null
		_round_manager = null
		_item_manager = null
		_item_interaction = null
		_shotgun = null
		_shell_spawner = null
		_intro = _find_script_node(scene, "IntroManager")
		_menu = _find_script_node(scene, "MenuManager")
		_last_stage = -1
		_last_round = -1
		_last_count = -1
	if _dealer == null or not is_instance_valid(_dealer):
		_dealer = _find_script_node(scene, "DealerIntelligence")
		if _dealer == null:
			return
		_round_manager = _dealer.roundManager
		print("AstrBotBridge: DealerIntelligence found")
		_item_manager = _dealer.itemManager
		_shotgun = _dealer.shotgunShooting
		_shell_spawner = _dealer.shellSpawner
		if _item_manager != null:
			_item_interaction = _item_manager.interaction
		if _intro == null:
			_intro = _find_script_node(scene, "IntroManager")


func _nodes_ready() -> bool:
	return (_dealer != null and is_instance_valid(_dealer)
		and _round_manager != null and is_instance_valid(_round_manager)
		and _shell_spawner != null and is_instance_valid(_shell_spawner)
		and _item_manager != null and is_instance_valid(_item_manager))


# ---------------------------------------------------------------- 开始对局

func _handle_start_match(_payload: Dictionary) -> void:
	if _starting:
		return
	_starting = true
	ai_controlled = true
	_game_over = false
	_result = ""
	_turn = "waiting"
	_last_state_json = ""
	_last_event = "game_started"
	_dealer_error_count = 0
	_reset_ai_knowledge()
	_last_stage = -1
	_last_round = -1
	_last_count = -1
	_round_started = false
	_auto_items_busy = false
	_auto_items_done = false
	var error := get_tree().change_scene_to_file(MAIN_SCENE)
	if error != OK:
		_send_error("无法加载主场景 %s：%s" % [MAIN_SCENE, error])
		_starting = false
		return
	await get_tree().create_timer(1.6, false).timeout
	_scene = null
	_dealer = null
	_intro = null
	_ensure_nodes()
	for _i in range(4):
		if _intro != null and is_instance_valid(_intro):
			break
		await get_tree().create_timer(0.6, false).timeout
		_ensure_nodes()
	if (_round_manager != null and is_instance_valid(_round_manager)
			and _round_manager.playerData != null):
		# 自动对局跳过豁免书/开场文本，直接进入赌桌。
		_round_manager.playerData.hasSignedWaiver = true
		_round_manager.playerData.hasReadIntroduction = true
		_round_manager.playerData.currentBatchIndex = 0
		_round_manager.dealerHasGreeted = true
		_round_manager.dealerAtTable = false
	if _intro != null and is_instance_valid(_intro) and _intro.has_method("BeginGame"):
		print("AstrBotBridge: BeginGame, dealer=", _dealer, " intro=", _intro)
		_intro.BeginGame()
	await get_tree().create_timer(0.8, false).timeout
	_ensure_nodes()
	_turn = "waiting"
	_waiting_since = 0
	_game_started = true
	_emit_state()
	_send_event("game_started")
	_starting = false


# ---------------------------------------------------------------- 帧流

func _stream_frame(delta: float) -> void:
	if not _connected:
		return
	_frame_timer += delta
	var interval := 0.05 if _turn == "animating" else 0.1
	if _frame_timer < interval:
		return
	_frame_timer = 0.0
	var viewport := get_viewport()
	if viewport == null:
		return
	var texture := viewport.get_texture()
	if texture == null:
		return
	var image := texture.get_image()
	if image == null or image.is_empty():
		return
	var jpg := image.save_jpg_to_buffer(0.65)
	if jpg.size() > 0:
		_send_bytes(jpg)


# ---------------------------------------------------------------- 状态

func _poll_state(delta: float) -> void:
	if not _nodes_ready():
		return
	_state_timer += delta
	if _state_timer < 0.25:
		return
	_state_timer = 0.0
	_tick_state()


func _tick_state() -> void:
	if not _nodes_ready():
		return
	if _round_manager.playerData == null:
		return
	var sequence: Array = _shell_spawner.sequenceArray
	var count := sequence.size()
	var stage := int(_round_manager.playerData.currentBatchIndex) + 1
	var round_number := int(_round_manager.currentRound) + 1
	if _last_stage < 0:
		print("AstrBotBridge: first state stage=", stage, " shells=", count, " turn=", _turn)
		_last_stage = stage
		_last_round = round_number
		_last_count = count
		_turn = "waiting"
		_waiting_since = 0
		_last_state_json = ""
		_send_event("new_loadout")
		_emit_state()
		return
	var loadout_changed := (stage != _last_stage or round_number != _last_round
		or (count > _last_count and count > 0))
	if loadout_changed:
		_last_stage = stage
		_last_round = round_number
		_last_count = count
		_reset_ai_knowledge()
		_round_started = false
		_auto_items_done = false
		_dealer_dead_reported = false
		_player_dead_reported = false
		_game_over = false
		_result = ""
		_dealer_error_count = 0
		_turn = "waiting"
		_waiting_since = 0
		_last_state_json = ""
		_last_event = "new_loadout"
		_send_event("new_loadout")
		_emit_state()
	else:
		if count == _last_count - 1:
			_shift_ai_after_pop()
		elif count < _last_count:
			_reset_ai_knowledge()
		_last_count = count

	var dealer_hp := int(_round_manager.health_opponent)
	var player_hp := int(_round_manager.health_player)
	if not _round_started:
		if count > 0 or dealer_hp > 0 or player_hp > 0:
			_round_started = true
			_auto_distribute_items()
		else:
			_emit_state()
			return
	if dealer_hp <= 0 and not _dealer_dead_reported:
		_dealer_dead_reported = true
		_last_event = "round_win"
		_send_event("round_win")
		if stage >= 3:
			_game_over = true
			_result = "win"
			_turn = "gameover"
			_send_event("game_over", {"result": "win"})
	elif player_hp <= 0 and not _player_dead_reported:
		_player_dead_reported = true
		_game_over = true
		_result = "loss"
		_turn = "gameover"
		_last_event = "round_lose"
		_send_event("round_lose")
		_send_event("game_over", {"result": "loss"})
	if _turn == "waiting" and count > 0 and not _game_over:
		if _waiting_since == 0:
			_waiting_since = Time.get_ticks_msec()
		elif Time.get_ticks_msec() - _waiting_since > 5000:
			# ShellLoader 未打补丁时的兜底：等 5 秒后允许玩家操作。
			_turn = "player"
			_waiting_since = 0
	else:
		_waiting_since = 0
	_emit_state()


func _public_state() -> Dictionary:
	if not _nodes_ready() or _round_manager.playerData == null:
		return {"stage": 0, "turn": "waiting", "game_over": false, "result": ""}
	var sequence: Array = _shell_spawner.sequenceArray
	var max_hp := 4
	var round_index := int(_round_manager.currentRound)
	if _round_manager.roundArray.size() > round_index:
		max_hp = int(_round_manager.roundArray[round_index].startingHealth)
	return {
		"stage": int(_round_manager.playerData.currentBatchIndex) + 1,
		"round": round_index + 1,
		"turn": ("gameover" if _game_over else _turn),
		"dealer_hp": int(_round_manager.health_opponent),
		"dealer_max_hp": max_hp,
		"player_hp": int(_round_manager.health_player),
		"player_max_hp": max_hp,
		"remaining_live_count": sequence.count("live"),
		"remaining_blank_count": sequence.count("blank"),
		"barrel_sawed_off": bool(_round_manager.barrelSawedOff),
		"player_handcuffed": bool(_round_manager.playerCuffed),
		"dealer_handcuffed": bool(_round_manager.dealerCuffed),
		"dealer_items": _items_for("dealer"),
		"player_items": _items_for("player"),
		"game_over": _game_over,
		"result": _result,
		"last_event": _last_event,
	}


func _dealer_view() -> Dictionary:
	var view := _public_state()
	var sequence: Array = _shell_spawner.sequenceArray if _shell_spawner != null else []
	var current := ""
	if _ai_known_current != "":
		current = _ai_known_current
	elif sequence.size() == 1:
		current = "LIVE" if sequence[0] == "live" else "BLANK"
	elif sequence.size() > 0:
		var live := sequence.count("live")
		var blank := sequence.count("blank")
		if live == sequence.size():
			current = "LIVE"
		elif blank == sequence.size():
			current = "BLANK"
	if current != "":
		view["current_chamber_shell"] = current
	var future := []
	for key in _ai_known_future.keys():
		var offset := int(key)
		if offset > 0 and offset < sequence.size():
			future.append({"offset": offset, "shell": _ai_known_future[key]})
	view["known_future"] = future
	return view


func _emit_state() -> void:
	if not _nodes_ready():
		return
	var state := _public_state()
	var encoded := JSON.stringify(state)
	if encoded == _last_state_json:
		return
	_last_state_json = encoded
	_send_json({"type": "state", "state": state})


func _send_event(name: String, extra: Dictionary = {}) -> void:
	var payload := {"type": "event", "event": name, "state": _public_state()}
	for key in extra.keys():
		payload[key] = extra[key]
	_send_json(payload)


func _send_error(message: String) -> void:
	_send_json({"type": "event", "event": "error", "message": message, "state": _public_state()})


# ---------------------------------------------------------------- 桌面道具

func _sort_by_slot(a, b) -> bool:
	return int(a["slot"]) < int(b["slot"])


func _player_item_nodes() -> Array:
	var out := []
	if _item_manager == null:
		return out
	var parent: Node = _item_manager.itemSpawnParent
	if parent == null:
		return out
	for child in parent.get_children():
		if child.get_child_count() < 2:
			continue
		var inter = child.get_child(1)
		if not _is_script(inter, "InteractionBranch"):
			continue
		if not bool(inter.isPlayerSide):
			continue
		out.append({
			"slot": int(inter.itemGridIndex),
			"item": str(inter.itemName),
			"node": child,
		})
	out.sort_custom(Callable(self, "_sort_by_slot"))
	return out


func _items_for(side: String) -> Array:
	var out := []
	if _item_manager == null:
		return out
	var parent: Node = _item_manager.itemSpawnParent
	if parent == null:
		return out
	var sequence_count := 0
	if _shell_spawner != null:
		sequence_count = _shell_spawner.sequenceArray.size()
	var dealer_slot := 0
	for child in parent.get_children():
		if child.get_child_count() < 2:
			continue
		var inter = child.get_child(1)
		if not _is_script(inter, "InteractionBranch"):
			continue
		var is_player := bool(inter.isPlayerSide)
		if side == "player" and not is_player:
			continue
		if side == "dealer" and is_player:
			continue
		var item_name := str(inter.itemName)
		# v1：肾上腺素需要二次选择偷取目标，网页暂不提供；两侧都不暴露。
		if item_name == "adrenaline":
			continue
		if side == "dealer":
			# 最后一发时啤酒/手铐/手机没有合法收益，从 AI 视野里隐藏。
			if sequence_count <= 1 and item_name in ["beer", "handcuffs", "burner phone"]:
				continue
			out.append({
				"slot": dealer_slot,
				"item": item_name,
				"label": ITEM_LABELS.get(item_name, item_name),
				"description": ITEM_DESCRIPTIONS.get(item_name, ""),
			})
			dealer_slot += 1
		else:
			out.append({
				"slot": int(inter.itemGridIndex),
				"item": item_name,
				"label": ITEM_LABELS.get(item_name, item_name),
				"description": ITEM_DESCRIPTIONS.get(item_name, ""),
			})
	if side == "player":
		out.sort_custom(Callable(self, "_sort_by_slot"))
	return out


func _find_player_item(slot_index: int):
	for item in _player_item_nodes():
		if int(item["slot"]) == slot_index:
			return item
	return null


# ---------------------------------------------------------------- 合法视野

func _reset_ai_knowledge() -> void:
	_ai_known_current = ""
	_ai_known_future = {}


func _shift_ai_after_pop() -> void:
	var new_current := ""
	var shifted: Dictionary = {}
	for key in _ai_known_future.keys():
		var offset := int(key)
		if offset <= 1:
			continue
		var shell: String = _ai_known_future[key]
		if offset == 2:
			new_current = shell
		else:
			shifted[offset - 1] = shell
	_ai_known_current = new_current
	_ai_known_future = shifted


func _record_dealer_glass(shell: String) -> void:
	_ai_known_current = "LIVE" if shell == "live" else "BLANK"


func choose_dealer_phone_reveal(sequence: Array) -> void:
	if sequence.size() <= 1:
		return
	var offset := randi_range(1, sequence.size() - 1)
	_ai_known_future[offset] = "LIVE" if sequence[offset] == "live" else "BLANK"


func player_peek_current(shell: String) -> void:
	_send_json({
		"type": "private_peek",
		"role": "player",
		"source": "magnifying_glass",
		"shell": "LIVE" if shell == "live" else "BLANK",
		"info": "当前枪膛内为：" + ("实弹 (LIVE)" if shell == "live" else "空包弹 (BLANK)"),
	})


func report_player_phone(index: int, shell: String) -> void:
	if not _player_phone_pending:
		return
	_send_json({
		"type": "private_peek",
		"role": "player",
		"source": "burner_phone",
		"shell": "LIVE" if shell == "live" else "BLANK",
		"offset": index,
		"info": "未来第 %d 发是：%s" % [index + 1, "实弹 (LIVE)" if shell == "live" else "空包弹 (BLANK)"],
	})


# ---------------------------------------------------------------- 动作分发

func _handle_action(payload: Dictionary) -> void:
	var role := str(payload.get("role", ""))
	var action := str(payload.get("action", ""))
	if role == "dealer":
		_request_inflight = false
		if action == "use_item":
			await _dealer_use_item(str(payload.get("item", "")))
		elif action == "shoot":
			await _dealer_shoot(str(payload.get("target", "")))
		else:
			_send_error("未知恶魔动作")
	elif role == "player":
		if action == "use_item":
			await _player_use_item(int(payload.get("slot_index", -1)))
		elif action == "shoot":
			await _player_shoot(str(payload.get("target", "")))
		elif action == "pickup_gun":
			await _player_pickup()


func _dealer_use_item(item: String) -> void:
	if _turn != "dealer":
		_send_error("现在不是恶魔回合")
		return
	if _dealer == null or not _dealer.has_method("AstrBotDealerUseItem"):
		_send_error("DealerIntelligence 未应用 AstrBot 补丁")
		return
	_turn = "animating"
	_emit_state()
	var before_count: int = _shell_spawner.sequenceArray.size()
	await _dealer.AstrBotDealerUseItem(item)
	if item == "inverter" and _ai_known_current != "":
		_ai_known_current = "BLANK" if _ai_known_current == "LIVE" else "LIVE"
	_sync_shell_delta(before_count)
	_emit_state()


func _dealer_shoot(target: String) -> void:
	if _turn != "dealer":
		_send_error("现在不是恶魔回合")
		return
	if target not in ["player", "self"]:
		_send_error("target 必须是 player 或 self")
		return
	if _dealer == null or not _dealer.has_method("AstrBotDealerShoot"):
		_send_error("DealerIntelligence 未应用 AstrBot 补丁")
		return
	_turn = "animating"
	_emit_state()
	var before_count: int = _shell_spawner.sequenceArray.size()
	await _dealer.AstrBotDealerShoot(target)
	_sync_shell_delta(before_count)
	_emit_state()


func _player_use_item(slot_index: int) -> void:
	if _turn != "player":
		_send_error("现在不是玩家回合")
		return
	var found: Variant = _find_player_item(slot_index)
	if found == null:
		_send_error("这个道具槽没有道具")
		return
	if _item_interaction == null or _item_interaction.get_parent() == null:
		_send_error("ItemInteraction 未就绪")
		return
	_turn = "animating"
	_emit_state()
	var item_name: String = found["item"]
	var node: Node = found["node"]
	_pending_player_item = item_name
	_player_phone_pending = item_name == "burner phone"
	var before_count: int = _shell_spawner.sequenceArray.size()
	await _item_interaction.PickupItemFromTable(node, item_name)
	_sync_shell_delta(before_count)
	if item_name == "magnifying glass" and _shell_spawner.sequenceArray.size() > 0:
		player_peek_current(_shell_spawner.sequenceArray[0])
	elif item_name == "handcuffs":
		_send_event("player_used_handcuffs")
	elif item_name == "handsaw":
		_send_event("player_used_handsaw")
	elif item_name == "inverter" and _ai_known_current != "":
		_ai_known_current = "BLANK" if _ai_known_current == "LIVE" else "LIVE"
	_pending_player_item = ""
	_player_phone_pending = false
	if int(_round_manager.health_player) <= 0:
		_turn = "gameover"
	elif _shell_spawner.sequenceArray.size() <= 0:
		_turn = "waiting"
		_waiting_since = 0
	else:
		_turn = "player"
	_emit_state()


func _player_shoot(target: String) -> void:
	if _turn != "player":
		_send_error("现在不是玩家回合")
		return
	if target not in ["dealer", "self"]:
		_send_error("target 必须是 dealer 或 self")
		return
	_turn = "animating"
	_emit_state()
	var sequence: Array = _shell_spawner.sequenceArray
	var shell := "blank"
	if sequence.size() > 0:
		shell = str(sequence[0])
	var damage := int(_round_manager.currentShotgunDamage)
	var before_count: int = sequence.size()
	if not _player_holds_gun:
		await _shotgun.GrabShotgun()
		await get_tree().create_timer(0.65, false).timeout
		_player_holds_gun = true
	await _shotgun.Shoot(target)
	_player_holds_gun = false
	_sync_shell_delta(before_count)
	var event_name := ""
	if target == "self":
		event_name = "player_shot_self_live" if shell == "live" else "player_shot_self_blank"
	else:
		event_name = "player_shot_dealer_live" if shell == "live" else "player_shot_dealer_blank"
	_last_event = event_name
	_send_event(event_name, {"shell": shell.to_upper(), "damage": damage})
	if int(_round_manager.health_player) <= 0:
		_turn = "gameover"
	_emit_state()


func _player_pickup() -> void:
	if _turn != "player":
		_send_error("现在不是玩家回合")
		return
	await _shotgun.GrabShotgun()
	_player_holds_gun = true
	_emit_state()


func _first_free_grid() -> int:
	if _item_manager == null:
		return -1
	var occupied = _item_manager.gridOccupiedArray
	for index in range(occupied.size()):
		if not bool(occupied[index]):
			return index
	return -1


func _auto_distribute_items() -> void:
	# 网页不展示“从手提箱拿道具”的点击阶段：由桥自动拿取并放到空网格。
	if _auto_items_busy or _auto_items_done or _item_manager == null:
		return
	var required := 0
	var round_index := int(_round_manager.currentRound)
	if _round_manager.roundArray.size() > round_index:
		required = int(_round_manager.roundArray[round_index].numberOfItemsToGrab)
	if required <= 0:
		_auto_items_done = true
		return
	if int(_item_manager.numberOfItemsGrabbed) >= required:
		_auto_items_done = true
		return
	var intake = _item_manager.interaction_intake
	if intake == null or not bool(intake.interactionAllowed):
		return
	_auto_items_busy = true
	var guard := 0
	while guard < required + 3 and int(_item_manager.numberOfItemsGrabbed) < required:
		guard += 1
		await _item_manager.GrabItem()
		var slot := _first_free_grid()
		if slot < 0:
			break
		await _item_manager.PlaceDownItem(slot)
	_auto_items_busy = false
	_auto_items_done = int(_item_manager.numberOfItemsGrabbed) >= required
	_emit_state()


func _sync_shell_delta(before_count: int) -> void:
	if _shell_spawner == null:
		return
	var now: int = _shell_spawner.sequenceArray.size()
	if now == before_count - 1:
		_shift_ai_after_pop()
	elif now < before_count:
		_reset_ai_knowledge()


# ---------------------------------------------------------------- 补丁回调

func on_dealer_turn() -> void:
	_ensure_nodes()
	if not _nodes_ready() or _game_over:
		return
	_turn = "dealer"
	_emit_state()
	if _request_inflight:
		_queued_dealer_request = true
		return
	_request_inflight = true
	_dealer_wait_started = Time.get_ticks_msec()
	_send_json({"type": "dealer_turn_request", "view": _dealer_view()})


func on_player_turn() -> void:
	_turn = "player"
	_request_inflight = false
	_queued_dealer_request = false
	_dealer_wait_started = 0
	_waiting_since = 0
	_emit_state()


func after_dealer_item(item_name: String) -> void:
	if _game_over:
		return
	var wait := 1.4 if item_name == "beer" else 0.35
	await get_tree().create_timer(wait, false).timeout
	_ensure_nodes()
	if _game_over or not _nodes_ready():
		return
	if int(_round_manager.health_opponent) <= 0:
		return
	if _queued_dealer_request:
		_queued_dealer_request = false
		_request_inflight = false
	if _dealer != null and _dealer.has_method("BeginDealerTurn"):
		_dealer.BeginDealerTurn()


func after_dealer_medicine() -> void:
	await get_tree().create_timer(0.3, false).timeout
	_ensure_nodes()
	if _game_over or not _nodes_ready():
		return
	if int(_round_manager.health_opponent) <= 0:
		return
	if _dealer != null and _dealer.has_method("BeginDealerTurn"):
		_dealer.BeginDealerTurn()


func on_manual_player_shot(_target: String) -> void:
	# 兼容玩家仍在游戏窗口里点击的情况；网页是主操作入口。
	_emit_state()


func report_action_error(message: String) -> void:
	_dealer_error_count += 1
	_send_error(message)
	_turn = "dealer"
	_emit_state()
	if _dealer_error_count >= 3:
		# 连续非法动作时退回原版 AI，避免整局卡死。
		ai_controlled = false
		if _dealer != null and _dealer.has_method("DealerChoice"):
			_dealer.DealerChoice()
		return
	if _dealer != null and _dealer.has_method("BeginDealerTurn"):
		await get_tree().create_timer(0.2, false).timeout
		_dealer.BeginDealerTurn()


func _poll_offline_fallback() -> void:
	if not ai_controlled or _connected or _turn != "dealer" or _game_over:
		return
	if _dealer_wait_started == 0:
		_dealer_wait_started = Time.get_ticks_msec()
		return
	if Time.get_ticks_msec() - _dealer_wait_started < 12000:
		return
	ai_controlled = false
	_dealer_wait_started = 0
	if _dealer != null and _dealer.has_method("DealerChoice"):
		_dealer.DealerChoice()
