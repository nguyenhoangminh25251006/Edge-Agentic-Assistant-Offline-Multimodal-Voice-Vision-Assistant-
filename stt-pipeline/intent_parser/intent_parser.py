"""Maps recognized speech transcripts to structured commands and parameters."""
import re
from typing import Any, Dict, Tuple

# Mapping of regex patterns to (intent_code, parameters)
INTENT_MAP: Dict[str, Tuple[str, Dict[str, Any]]] = {
    # Emergency and safety commands (CMD_ESTOP stays strictly above CMD_STOP)
    r"\b(emergency stop|e stop|emergency)\b": ("CMD_ESTOP", {}),
    r"\b(stop belt|stop conveyor|stop line|halt|stop)\b": ("CMD_STOP", {}),

    # Conveyor line control
    r"\b(start belt|start conveyor|start line|start)\b": ("CMD_START", {}),
    r"\b(pause belt|pause line|pause)\b": ("CMD_PAUSE", {}),
    r"\b(resume belt|resume line|resume)\b": ("CMD_RESUME", {}),

    # Speed adjustments
    r"\b(speed up|faster|increase speed)\b": ("CMD_ADJUST_SPEED", {"delta": +20}),
    r"\b(slow down|slower|decrease speed)\b": ("CMD_ADJUST_SPEED", {"delta": -20}),
    r"\b(set speed slow|speed sixty|speed low)\b": ("CMD_SET_SPEED", {"target": 60}),
    r"\b(set speed normal|speed medium|speed one twenty|normal speed)\b": (
        "CMD_SET_SPEED",
        {"target": 120},
    ),
    r"\b(set speed fast|speed high|speed two hundred|fast speed)\b": (
        "CMD_SET_SPEED",
        {"target": 200},
    ),

    # Maintenance and status queries
    r"\b(clear jam|jam cleared|unblock|clear blockage)\b": ("CMD_CLEAR_JAM", {}),
    r"\b(reset fault|clear fault|fault reset|clear error)\b": ("CMD_RESET_FAULT", {}),
    r"\b(system status|get status|status report|status)\b": ("QUERY_STATUS", {}),
}


class IntentParser:
    """Parses transcript text into canonical command intent codes."""

    @staticmethod
    def parse(transcript: str) -> Tuple[str, Dict[str, Any]]:
        """Match transcript string against defined regex command patterns."""
        cleaned = transcript.lower().strip()
        for pattern, (intent, params) in INTENT_MAP.items():
            if re.search(pattern, cleaned):
                return intent, params
        return "UNKNOWN_INTENT", {}

    @staticmethod
    def all_grammar_phrases() -> list[str]:
        """Extract a deduplicated list of recognized phrase patterns for grammar initialization."""
        phrases: list[str] = []
        for pattern in INTENT_MAP:
            core = re.sub(r"\\b|\(|\)", "", pattern)
            alternatives = [a.strip() for a in core.split("|") if a.strip()]
            phrases.extend(alternatives)
        return list(dict.fromkeys(phrases))
