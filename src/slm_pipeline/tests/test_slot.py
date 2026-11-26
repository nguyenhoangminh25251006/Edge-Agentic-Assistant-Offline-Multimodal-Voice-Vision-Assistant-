"""test_slot.py — Table-driven and extended tests for deterministic speed slot extraction (U-3)."""

import pytest
from slm_pipeline.config import SlotConfig
from slm_pipeline.slot import SlotError, SpeedSlot, extract_speed


@pytest.mark.parametrize(
    ("text", "rated_mms", "require_unit", "expected"),
    [
        ("set the speed to two hundred millimeters per second", None, True, 200),
        ("speed 250 mm per second", None, True, 250),
        ("twenty five centimeters per second", None, True, 250),
        ("point five meters per second", None, True, 500),
        ("set speed to 200", None, True, SlotError.NEED_UNIT),
        ("fifty percent", 300, True, 150),
        ("fifty percent", None, True, SlotError.NO_RATED_SPEED),
        ("make it faster", None, True, SlotError.NONE_FOUND),
        ("two hundred or three hundred millimeters per second", None, True, SlotError.AMBIGUOUS),
        ("ten thousand millimeters per second", None, True, SlotError.OUT_OF_PARSE_RANGE),
        # Extra test cases
        ("three point five meters per second", None, True, 3500),
        ("12.5 cm/s", None, True, 125),
        ("one hundred and twenty mm/s", None, True, 120),
        ("seventy five percent", 400, True, 300),
        ("slow down a bit", None, True, SlotError.NONE_FOUND),
        ("run at 100 mm/s and 100 mm/s", None, True, 100),  # duplicate same setpoint is OK
        ("", None, True, SlotError.NONE_FOUND),
    ],
)
def test_extract_speed_table(text, rated_mms, require_unit, expected):
    cfg = SlotConfig(rated_mms=rated_mms, require_unit=require_unit)
    res = extract_speed(text, cfg)

    if isinstance(expected, SlotError):
        assert res == expected, f"Expected error {expected}, got {res}"
    else:
        assert isinstance(res, SpeedSlot), f"Expected SpeedSlot, got {res}"
        assert res.setpoint_mms == expected


def test_extract_speed_digits_and_units():
    cfg = SlotConfig(require_unit=True)
    res = extract_speed("conveyor speed 450 mm/s", cfg)
    assert isinstance(res, SpeedSlot)
    assert res.setpoint_mms == 450
    assert res.unit_in == "mm/s"
