#!/usr/bin/env python3
"""把 AstrBotBridge 接入反编译后的《恶魔轮盘赌》Godot 工程。

用法：
  python3 godot_patch/apply_patch.py /path/to/decompiled_project

只修改脚本；AstrBotBridge.gd 会被复制到工程的 godot_patch/ 下。
建议先备份工程或先复制一份。
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

BRIDGE_SOURCE = Path(__file__).resolve().parent / "AstrBotBridge.gd"

DEALER_HOOK = '''func BeginDealerTurn():
	var astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
	if astrbot_bridge != null and astrbot_bridge.ai_controlled:
		astrbot_bridge.on_dealer_turn()
		return
	mainLoopFinished = false
'''

DEALER_METHODS = '''

# ---------------------------------------------------------------- AstrBot bridge

func AstrBotDealerUseItem(itemName: String) -> void:
	var bridge = get_node_or_null("/root/AstrBotBridge")
	if itemName == "" or itemName not in itemManager.itemArray_dealer:
		if bridge != null:
			bridge.report_action_error("恶魔没有道具 " + itemName)
		return
	if dealerHoldingShotgun:
		animator_shotgun.play("enemy put down shotgun")
		shellLoader.DealerHandsDropShotgun()
		dealerHoldingShotgun = false
		await get_tree().create_timer(.45, false).timeout
	if roundManager.waitingForDealerReturn:
		await get_tree().create_timer(1.8, false).timeout
		roundManager.waitingForDealerReturn = false
	if itemName == "beer" and shellSpawner.sequenceArray.size() <= 1:
		if bridge != null:
			bridge.report_action_error("最后一发不能使用啤酒")
		return
	var amountArray : Array[AmountResource] = amounts.array_amounts
	for res in amountArray:
		if res.itemName == itemName:
			res.amount_dealer -= 1
			break
	match itemName:
		"handsaw":
			roundManager.barrelSawedOff = true
			roundManager.currentShotgunDamage = 2
		"handcuffs":
			roundManager.playerCuffed = true
		"beer":
			shellEject_dealer.FadeOutShell()
		"inverter":
			if shellSpawner.sequenceArray.size() > 0:
				shellSpawner.sequenceArray[0] = "blank" if shellSpawner.sequenceArray[0] == "live" else "live"
		"expired medicine":
			medicine.dealerDying = randf_range(0.0, 1.0) >= .5
	await hands.PickupItemFromTable(itemName)
	itemManager.itemArray_dealer.erase(itemName)
	itemManager.numberOfItemsGrabbed_enemy -= 1
	if bridge != null:
		if itemName == "magnifying glass" and shellSpawner.sequenceArray.size() > 0:
			bridge.record_dealer_glass(shellSpawner.sequenceArray[0])
		elif itemName == "burner phone":
			bridge.choose_dealer_phone_reveal(shellSpawner.sequenceArray)
	if itemName == "expired medicine":
		return
	if bridge != null:
		await bridge.after_dealer_item(itemName)

func AstrBotDealerShoot(who: String) -> void:
	var bridge = get_node_or_null("/root/AstrBotBridge")
	if who not in ["self", "player"]:
		if bridge != null:
			bridge.report_action_error("非法开枪目标")
		return
	if not dealerHoldingShotgun:
		await GrabShotgun()
		await get_tree().create_timer(1.4 + .5 - 1, false).timeout
	await get_tree().create_timer(.2, false).timeout
	await Shoot(who)
'''

ROUND_HOOK = '''	perm.SetInteractionPermissions(true)
	SetupDeskUI()

	var astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
	if astrbot_bridge != null:
		astrbot_bridge.on_player_turn()

@export var deskUI_parent : Control'''

SHELL_LOADER_HOOK = '''	perm.SetInteractionPermissions(true)
	roundManager.SetupDeskUI()
	var astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
	if astrbot_bridge != null:
		astrbot_bridge.on_player_turn()
	pass'''

MEDICINE_REPLACEMENT = '''\t\tvar astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
\t\tif astrbot_bridge != null and astrbot_bridge.ai_controlled:
\t\t\tawait astrbot_bridge.after_dealer_medicine()
\t\telse:
\t\t\tdealerai.DealerChoice()'''

BURNER_HOOK = '''		if (sequence[randindex] == "blank"): secondpart = tr("BLANKROUND") % ""
		else: secondpart = tr("LIVEROUND") % ""
		var astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
		if astrbot_bridge != null:
			astrbot_bridge.report_player_phone(randindex, sequence[randindex])
		match (randindex):'''

INTERACTION_METHODS = '''

func AstrBotPlayerAction(alias : String):
	InteractWith(alias)

func AstrBotBridgeNotify(target : String):
	var bridge = get_node_or_null("/root/AstrBotBridge")
	if bridge != null and bridge.has_method("on_manual_player_shot"):
		bridge.on_manual_player_shot(target)
'''

GLOBAL_HOOK = '''func _ready():
	if using_steam: currentVersion = currentVersion_nr + versuffix_steam
	else: currentVersion = currentVersion_nr + versuffix_itch
	var astrbot_bridge = get_node_or_null("/root/AstrBotBridge")
	if astrbot_bridge == null:
		var bridge_script = load("res://godot_patch/AstrBotBridge.gd")
		if bridge_script != null:
			astrbot_bridge = bridge_script.new()
			astrbot_bridge.name = "AstrBotBridge"
			get_tree().root.add_child.call_deferred(astrbot_bridge)'''


def _replace(path: Path, old: str, new: str, *, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    actual = text.count(old)
    if actual < count:
        raise SystemExit(f"{path}: 找不到预期补丁锚点（找到 {actual}，需要 {count}）")
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def patch_project(root: Path) -> None:
    scripts = root / "scripts"
    dealer = scripts / "DealerIntelligence.gd"
    round_mgr = scripts / "RoundManager.gd"
    shell_loader = scripts / "ShellLoader.gd"
    medicine = scripts / "MedicineManager.gd"
    burner = scripts / "BurnerPhone.gd"
    interaction = scripts / "InteractionManager.gd"
    global_vars = scripts / "GlobalVariables.gd"
    steam = scripts / "Steam.gd"
    for path in (dealer, round_mgr, shell_loader, medicine, burner, interaction, global_vars):
        if not path.exists():
            raise SystemExit(f"缺少脚本：{path}")

    _replace(dealer, "func BeginDealerTurn():\n\tmainLoopFinished = false\n", DEALER_HOOK)
    with dealer.open("a", encoding="utf-8") as handle:
        handle.write(DEALER_METHODS)

    _replace(round_mgr, "	perm.SetInteractionPermissions(true)\n	SetupDeskUI()\n\n@export var deskUI_parent : Control",
             ROUND_HOOK)
    _replace(shell_loader, "	perm.SetInteractionPermissions(true)\n	roundManager.SetupDeskUI()\n	pass",
             SHELL_LOADER_HOOK)

    medicine_text = medicine.read_text(encoding="utf-8")
    medicine_text, changed = re.subn(
        r"^(\s*)dealerai\.DealerChoice\(\)$", MEDICINE_REPLACEMENT, medicine_text, flags=re.M)
    if changed != 2:
        raise SystemExit(f"MedicineManager: 预期 2 处 DealerChoice，找到 {changed}")
    medicine.write_text(medicine_text, encoding="utf-8")

    _replace(burner, '''		if (sequence[randindex] == "blank"): secondpart = tr("BLANKROUND") % ""
		else: secondpart = tr("LIVEROUND") % ""
		match (randindex):''', BURNER_HOOK)

    _replace(interaction, '''		"text dealer":
			if (!fs_dec):
				shotgun.Shoot("dealer")''', '''		"text dealer":
			if (!fs_dec):
				AstrBotBridgeNotify("dealer")
				shotgun.Shoot("dealer")''')
    _replace(interaction, '''		"text you":
			if (!fs_dec):
				shotgun.Shoot("self")''', '''		"text you":
			if (!fs_dec):
				AstrBotBridgeNotify("self")
				shotgun.Shoot("self")''')
    with interaction.open("a", encoding="utf-8") as handle:
        handle.write(INTERACTION_METHODS)

    _replace(global_vars, '''func _ready():
	if using_steam: currentVersion = currentVersion_nr + versuffix_steam
	else: currentVersion = currentVersion_nr + versuffix_itch''', GLOBAL_HOOK)

    # 使用官方 Godot 运行时加载 PCK 时，GodotSteam 不会存在；动态查找即可，
    # using_steam=false 时根本不会调用，不影响正常逻辑。
    if steam.exists():
        text = steam.read_text(encoding="utf-8")
        text = text.replace("if GlobalVariables.using_steam: Steam.run_callbacks()",
                            "if GlobalVariables.using_steam:\n\t\tvar steam_singleton = Engine.get_singleton(\"Steam\")\n\t\tif steam_singleton != null: steam_singleton.run_callbacks()")
        text = text.replace("var INIT: Dictionary = Steam.steamInit(false)",
                            "var steam_singleton = Engine.get_singleton(\"Steam\")\n\t\t\tvar INIT: Dictionary = steam_singleton.steamInit(false) if steam_singleton != null else {}")
        steam.write_text(text, encoding="utf-8")

    target = root / "godot_patch" / "AstrBotBridge.gd"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BRIDGE_SOURCE, target)
    print(f"patched {root}")
    print("  DealerIntelligence.gd / RoundManager.gd / ShellLoader.gd / MedicineManager.gd")
    print("  BurnerPhone.gd / InteractionManager.gd / GlobalVariables.gd")
    print(f"  bridge -> {target}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(1)
    patch_project(Path(sys.argv[1]).expanduser().resolve())
