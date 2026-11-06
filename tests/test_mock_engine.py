from __future__ import annotations

from game.decision import fallback_action
from game.mock_engine import MockBuckshotGame
from game.practice import choose_player_action


def test_start_and_public_view_are_sanitized():
    game = MockBuckshotGame(seed=7)
    events = game.start()
    assert [event["event"] for event in events][0] == "new_loadout"
    assert game.turn == "player"
    view = game.dealer_view()
    assert "sequence" not in view
    assert "known_future" in view
    assert view["remaining_live_count"] + view["remaining_blank_count"] == len(game.shells)
    public = game.public_state()
    assert "sequence" not in public


def test_player_glass_is_private_and_not_in_dealer_view():
    game = MockBuckshotGame(seed=3)
    game.shells = ["LIVE", "BLANK"]
    game.player.items = ["magnifying glass", "beer"]
    game.dealer.known_current = None
    events = game.player_action({"action": "use_item", "slot_index": 0})
    peek = [event for event in events if event["event"] == "private_peek"]
    assert peek and peek[0]["role"] == "player" and peek[0]["shell"] == "LIVE"
    assert game.dealer_view().get("current_chamber_shell") is None


def test_dealer_glass_unlocks_current_shell():
    game = MockBuckshotGame(seed=3)
    game.shells = ["LIVE", "BLANK"]
    game.dealer.items = ["magnifying glass"]
    game.turn = "dealer"
    game.dealer_action({"action": "use_item", "item": "magnifying glass"})
    view = game.dealer_view()
    assert view["current_chamber_shell"] == "LIVE"
    assert "sequence" not in view


def test_handcuffs_skip_dealer_turn():
    game = MockBuckshotGame(seed=3)
    game.shells = ["BLANK", "LIVE"]
    game.player.items = ["handcuffs"]
    game.dealer.cuffed = False
    game.player_action({"action": "use_item", "slot_index": 0})
    assert game.dealer.cuffed is True
    game.player_action({"action": "shoot", "target": "dealer"})
    assert game.turn == "player"
    assert game.dealer.cuffed is False
    assert any("手铐" in line for line in game.log)


def test_handsaw_damage():
    game = MockBuckshotGame(seed=3)
    game.shells = ["LIVE", "BLANK"]
    game.player.items = ["handsaw"]
    game.dealer.hp = 4
    game.dealer.max_hp = 4
    game.player_action({"action": "use_item", "slot_index": 0})
    assert game.barrel_sawed_off and game.damage == 2
    game.player_action({"action": "shoot", "target": "dealer"})
    assert game.dealer.hp == 2


def test_beer_on_last_shell_loads_new_round():
    game = MockBuckshotGame(seed=3)
    game.shells = ["LIVE"]
    game.player.items = ["beer"]
    events = game.player_action({"action": "use_item", "slot_index": 0})
    assert any(event["event"] == "new_loadout" for event in events)
    assert len(game.shells) >= 2
    assert game.turn == "player"


def test_balanced_hp_by_stage():
    game = MockBuckshotGame(seed=1)
    game.start()
    assert (game.player.hp, game.dealer.hp) == (3, 3)
    game._begin_stage(2)
    assert (game.player.hp, game.dealer.hp) == (3, 3)
    game._begin_stage(3)
    assert (game.player.hp, game.dealer.hp) == (4, 4)

    legacy = MockBuckshotGame(seed=1, balanced_hp=False)
    legacy.start()
    legacy._begin_stage(2)
    assert (legacy.player.hp, legacy.dealer.hp) == (3, 4)


def test_shot_state_already_reflects_next_turn():
    game = MockBuckshotGame(seed=3)
    game.player.items = []
    game.dealer.items = []
    game.shells = ["BLANK", "LIVE"]
    events = game.player_action({"action": "shoot", "target": "dealer"})
    shot = next(event for event in events if event["event"] == "player_shot_dealer_blank")
    assert shot["state"]["turn"] == "dealer"
    assert game.turn == "dealer"

    game2 = MockBuckshotGame(seed=3)
    game2.player.items = []
    game2.dealer.items = []
    game2.shells = ["BLANK", "LIVE"]
    events2 = game2.player_action({"action": "shoot", "target": "self"})
    shot2 = next(event for event in events2 if event["event"] == "player_shot_self_blank")
    assert shot2["state"]["turn"] == "player"
    assert game2.turn == "player"

    game3 = MockBuckshotGame(seed=3)
    game3.player.items = []
    game3.dealer.items = []
    game3.shells = ["BLANK", "LIVE"]
    game3.turn = "dealer"
    events3 = game3.dealer_action({"action": "shoot", "target": "player"})
    shot3 = next(event for event in events3 if event["event"] == "dealer_shot_player_blank")
    assert shot3["state"]["turn"] == "player"
    assert game3.turn == "player"


def test_out_of_turn_player_action_is_rejected():
    game = MockBuckshotGame(seed=3)
    game.shells = ["LIVE", "BLANK"]
    game.player_action({"action": "shoot", "target": "dealer"})
    turn_after_shot = game.turn
    assert turn_after_shot == "dealer"
    try:
        game.player_action({"action": "shoot", "target": "dealer"})
    except Exception as exc:
        assert "回合" in str(exc)
    else:
        raise AssertionError("out-of-turn action should be rejected")


def test_full_random_play_finishes():
    game = MockBuckshotGame(seed=42)
    game.start()
    turns = 0
    while not game.game_over and turns < 240:
        turns += 1
        if game.turn == "player":
            action = choose_player_action(game.player_view())
            game.player_action(action)
        else:
            view = game.dealer_view()
            game.dealer_action(fallback_action(view))
    assert game.game_over
    assert game.result in ("win", "loss")
    assert turns < 240
