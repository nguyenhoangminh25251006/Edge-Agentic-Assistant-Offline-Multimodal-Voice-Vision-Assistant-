"""Unit tests for the IntentParser module."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from intent_parser import IntentParser


def _parse(text: str):
    return IntentParser.parse(text)


def test_emergency_stop():
    assert _parse("emergency stop")[0] == "CMD_ESTOP"


def test_e_stop_shorthand():
    assert _parse("e stop")[0] == "CMD_ESTOP"


def test_stop_belt():
    assert _parse("stop belt")[0] == "CMD_STOP"


def test_stop_conveyor():
    assert _parse("stop conveyor")[0] == "CMD_STOP"


def test_halt():
    assert _parse("halt")[0] == "CMD_STOP"


def test_start_belt():
    assert _parse("start belt")[0] == "CMD_START"


def test_start_line():
    assert _parse("start line")[0] == "CMD_START"


def test_pause():
    assert _parse("pause")[0] == "CMD_PAUSE"


def test_pause_belt():
    assert _parse("pause belt")[0] == "CMD_PAUSE"


def test_resume():
    assert _parse("resume")[0] == "CMD_RESUME"


def test_resume_line():
    assert _parse("resume line")[0] == "CMD_RESUME"


def test_increase_speed():
    code, params = _parse("increase speed")
    assert code == "CMD_ADJUST_SPEED"
    assert params["delta"] == 20


def test_speed_up():
    code, params = _parse("speed up")
    assert code == "CMD_ADJUST_SPEED"
    assert params["delta"] == 20


def test_decrease_speed():
    code, params = _parse("decrease speed")
    assert code == "CMD_ADJUST_SPEED"
    assert params["delta"] == -20


def test_slower():
    code, params = _parse("slower")
    assert code == "CMD_ADJUST_SPEED"
    assert params["delta"] == -20


def test_set_speed_slow():
    code, params = _parse("set speed slow")
    assert code == "CMD_SET_SPEED"
    assert params["target"] == 60


def test_set_speed_normal():
    code, params = _parse("set speed normal")
    assert code == "CMD_SET_SPEED"
    assert params["target"] == 120


def test_set_speed_fast():
    code, params = _parse("set speed fast")
    assert code == "CMD_SET_SPEED"
    assert params["target"] == 200


def test_clear_jam():
    assert _parse("clear jam")[0] == "CMD_CLEAR_JAM"


def test_unblock():
    assert _parse("unblock")[0] == "CMD_CLEAR_JAM"


def test_reset_fault():
    assert _parse("reset fault")[0] == "CMD_RESET_FAULT"


def test_clear_error():
    assert _parse("clear error")[0] == "CMD_RESET_FAULT"


def test_system_status():
    assert _parse("system status")[0] == "QUERY_STATUS"


def test_get_status():
    assert _parse("get status")[0] == "QUERY_STATUS"


def test_unknown():
    assert _parse("open the pod bay doors")[0] == "UNKNOWN_INTENT"


def test_empty_string():
    assert _parse("")[0] == "UNKNOWN_INTENT"


def test_grammar_phrases_minimum_count():
    phrases = IntentParser.all_grammar_phrases()
    assert len(phrases) >= 10


def test_intent_parser_fail_safe_stop_priority():
    """Verify that emergency stop commands take precedence and bare 'stop' is recognized."""
    assert _parse("emergency stop")[0] == "CMD_ESTOP"
    assert _parse("e stop")[0] == "CMD_ESTOP"
    assert _parse("stop")[0] == "CMD_STOP"
    assert _parse("halt")[0] == "CMD_STOP"
    assert _parse("please stop conveyor")[0] == "CMD_STOP"
