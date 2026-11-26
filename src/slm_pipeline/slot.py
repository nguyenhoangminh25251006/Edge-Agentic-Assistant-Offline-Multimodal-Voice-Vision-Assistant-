"""slot.py — Deterministic speed slot extraction from speech utterances."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import enum
import math
import re
from typing import Literal

from slm_pipeline.config import SlotConfig


class SlotError(str, enum.Enum):
    NONE_FOUND = "NONE_FOUND"
    NEED_UNIT = "NEED_UNIT"
    AMBIGUOUS = "AMBIGUOUS"
    NO_RATED_SPEED = "NO_RATED_SPEED"
    OUT_OF_PARSE_RANGE = "OUT_OF_PARSE_RANGE"


@dataclass(frozen=True)
class SpeedSlot:
    setpoint_mms: int
    unit_in: Literal["mm/s", "cm/s", "m/s", "percent"]
    raw_span: str


# Number words to digit mappings
_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}

_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

_SCALES = {
    "hundred": 100,
    "thousand": 1000,
}

# Unit regex patterns
_UNIT_PATTERNS = [
    (r"\b(?:millimeters?\s+per\s+second|mm\s+per\s+second|mm/s)\b", "mm/s", 1.0),
    (r"\b(?:centimeters?\s+per\s+second|cm\s+per\s+second|cm/s)\b", "cm/s", 10.0),
    (r"\b(?:meters?\s+per\s+second|m\s+per\s+second|m/s)\b", "m/s", 1000.0),
    (r"\b(?:percent|%)\b", "percent", None),
]


def _parse_word_number(tokens: list[str]) -> float | None:
    """Parse a sequence of number words into a float (e.g. ['one', 'hundred', 'and', 'twenty'])."""
    total = 0.0
    current = 0.0
    has_digits = False

    # Check for decimal part: e.g. "three point five" or "point five"
    if "point" in tokens:
        idx = tokens.index("point")
        integer_tokens = tokens[:idx]
        decimal_tokens = tokens[idx + 1 :]

        int_val = _parse_word_number(integer_tokens) if integer_tokens else 0.0
        if int_val is None:
            return None

        dec_str = ""
        for t in decimal_tokens:
            if t in _ONES:
                dec_str += str(_ONES[t])
            else:
                return None
        if not dec_str:
            return None
        return int_val + float(f"0.{dec_str}")

    for token in tokens:
        if token == "and":
            continue
        if token in _ONES:
            current += _ONES[token]
            has_digits = True
        elif token in _TENS:
            current += _TENS[token]
            has_digits = True
        elif token == "hundred":
            if current == 0:
                current = 1
            current *= 100
            has_digits = True
        elif token == "thousand":
            if current == 0:
                current = 1
            total += current * 1000
            current = 0
            has_digits = True
        else:
            return None

    if not has_digits:
        return None
    return total + current


def _round_half_up(val: float) -> int:
    return int(Decimal(str(val)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _parse_number_from_text(text: str) -> tuple[float | None, list[str]]:
    """Extract a number (digit or word-based) from the suffix of text."""
    tokens = re.findall(r"[a-z0-9.]+", text.strip().lower())
    if not tokens:
        return None, []
    for window_len in range(min(len(tokens), 8), 0, -1):
        sub_tokens = tokens[-window_len:]
        if len(sub_tokens) == 1:
            try:
                val = float(sub_tokens[0])
                return val, sub_tokens
            except ValueError:
                pass
        val = _parse_word_number(sub_tokens)
        if val is not None:
            return val, sub_tokens
    return None, []


def extract_speed(text: str, cfg: SlotConfig, locale: str = "en") -> SpeedSlot | SlotError:
    """Extract speed slot from speech text deterministically."""
    if not text:
        return SlotError.NONE_FOUND

    # Rule 8: Relative words return NONE_FOUND
    lower = text.lower()
    if re.search(r"\b(?:faster|slower|a\s+bit\s+quicker|more\s+speed)\b", lower):
        return SlotError.NONE_FOUND

    # Find all unit matches with their spans
    unit_matches: list[tuple[int, int, str, float | None, str]] = []
    for pattern, unit_name, multiplier in _UNIT_PATTERNS:
        for m in re.finditer(pattern, lower):
            unit_matches.append((m.start(), m.end(), unit_name, multiplier, m.group(0)))

    # Sort units by appearance
    unit_matches.sort(key=lambda x: x[0])

    candidates: list[SpeedSlot | SlotError] = []

    # Check for unit matches and parse number immediately preceding the unit
    for start, end, unit_name, multiplier, unit_str in unit_matches:
        preceding = lower[:start].strip()

        # Check if preceding text has multiple number clauses (e.g. "200 or 300")
        clauses = [c.strip() for c in re.split(r"\b(?:or|between|to)\b", preceding) if c.strip()]
        parsed_numbers: list[tuple[float, list[str]]] = []

        if len(clauses) > 1:
            for clause in clauses:
                val, m_toks = _parse_number_from_text(clause)
                if val is not None:
                    parsed_numbers.append((val, m_toks))

        if not parsed_numbers:
            val, m_toks = _parse_number_from_text(preceding)
            if val is not None:
                parsed_numbers.append((val, m_toks))

        for parsed_val, matched_tokens in parsed_numbers:
            raw_span = f"{' '.join(matched_tokens)} {unit_str}"
            if parsed_val < 0 or parsed_val >= 10000:
                candidates.append(SlotError.OUT_OF_PARSE_RANGE)
                continue

            if unit_name == "percent":
                if cfg.rated_mms is None:
                    candidates.append(SlotError.NO_RATED_SPEED)
                else:
                    mms = _round_half_up(cfg.rated_mms * parsed_val / 100.0)
                    candidates.append(SpeedSlot(setpoint_mms=mms, unit_in="percent", raw_span=raw_span))
            else:
                mms = _round_half_up(parsed_val * multiplier)
                candidates.append(SpeedSlot(setpoint_mms=mms, unit_in=unit_name, raw_span=raw_span))

    # If no unit match found, check if a bare number exists
    if not candidates:
        # Scan for digit-based numbers
        digit_matches = re.findall(r"\b\d+(?:\.\d+)?\b", lower)
        # Or number words
        all_words = re.findall(r"[a-z0-9.]+", lower)
        has_number = len(digit_matches) > 0
        if not has_number:
            for w in all_words:
                if w in _ONES or w in _TENS:
                    has_number = True
                    break

        if has_number:
            if cfg.require_unit:
                return SlotError.NEED_UNIT
            else:
                return SlotError.NONE_FOUND
        return SlotError.NONE_FOUND

    # Check candidates
    valid_slots = [c for c in candidates if isinstance(c, SpeedSlot)]
    errors = [c for c in candidates if isinstance(c, SlotError)]

    if len(valid_slots) > 1:
        # Check if they have the same setpoint_mms (e.g. duplicates)
        distinct = {s.setpoint_mms for s in valid_slots}
        if len(distinct) > 1:
            return SlotError.AMBIGUOUS
        return valid_slots[0]

    if len(valid_slots) == 1:
        if len(candidates) > 1:
            # If mixed with out of range / errors
            return SlotError.AMBIGUOUS
        return valid_slots[0]

    # Only errors
    if SlotError.OUT_OF_PARSE_RANGE in errors:
        return SlotError.OUT_OF_PARSE_RANGE
    if SlotError.NO_RATED_SPEED in errors:
        return SlotError.NO_RATED_SPEED
    return errors[0]
