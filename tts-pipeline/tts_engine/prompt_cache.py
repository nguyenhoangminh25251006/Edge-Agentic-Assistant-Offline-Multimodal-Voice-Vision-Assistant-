"""In-memory audio prompt cache manager.

Provides sub-millisecond playback retrieval for frequent and safety-critical
audio alerts purely in RAM, without saving .wav files to disk.
"""
import re
import wave
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

import numpy as np

try:
    from config import DEFAULT_SAMPLE_RATE
    from .base import BaseTTSEngine
except ImportError:
    from ..config import DEFAULT_SAMPLE_RATE
    from .base import BaseTTSEngine


# Canonical prompt definitions: (identifier, standard spoken text)
STANDARD_PROMPTS: Dict[str, str] = {
    "EMERGENCY_STOP": "Emergency stop activated. Conveyor halted.",
    "LINE_STARTED": "Conveyor belt started.",
    "LINE_STOPPED": "Conveyor belt stopped.",
    "LINE_PAUSED": "Conveyor belt paused.",
    "LINE_RESUMED": "Conveyor belt resumed.",
    "DEFECT_DETECTED": "Warning. Defect detected on printed circuit board.",
    "JAM_DETECTED": "Board jam detected. Please inspect conveyor rail.",
    "OVERLOAD_WARNING": "Warning. Motor current limit exceeded.",
    "SYSTEM_READY": "System online and ready.",
}

# Alias mapping from normalized text variants to canonical prompt keys
PROMPT_ALIASES: Dict[str, str] = {
    # Emergency stop variants
    "emergency stop": "EMERGENCY_STOP",
    "emergency stop activated": "EMERGENCY_STOP",
    "emergency stop activated conveyor halted": "EMERGENCY_STOP",
    "e stop": "EMERGENCY_STOP",
    "halt": "EMERGENCY_STOP",
    # Line started variants
    "conveyor started": "LINE_STARTED",
    "conveyor belt started": "LINE_STARTED",
    "line started": "LINE_STARTED",
    "line run": "LINE_STARTED",
    # Line stopped variants
    "conveyor stopped": "LINE_STOPPED",
    "conveyor belt stopped": "LINE_STOPPED",
    "line stopped": "LINE_STOPPED",
    # Line paused variants
    "conveyor paused": "LINE_PAUSED",
    "conveyor belt paused": "LINE_PAUSED",
    "line paused": "LINE_PAUSED",
    # Line resumed variants
    "conveyor resumed": "LINE_RESUMED",
    "conveyor belt resumed": "LINE_RESUMED",
    "line resumed": "LINE_RESUMED",
    # Defect variants
    "defect detected": "DEFECT_DETECTED",
    "defect detected on printed circuit board": "DEFECT_DETECTED",
    "warning defect detected on printed circuit board": "DEFECT_DETECTED",
    "pcb defect detected": "DEFECT_DETECTED",
    # Jam variants
    "jam detected": "JAM_DETECTED",
    "board jam detected": "JAM_DETECTED",
    "board jam detected please inspect conveyor rail": "JAM_DETECTED",
    # Overload variants
    "overload warning": "OVERLOAD_WARNING",
    "motor overload": "OVERLOAD_WARNING",
    "warning motor current limit exceeded": "OVERLOAD_WARNING",
    "motor current limit exceeded": "OVERLOAD_WARNING",
    # System ready variants
    "system ready": "SYSTEM_READY",
    "system online": "SYSTEM_READY",
    "system online and ready": "SYSTEM_READY",
}


def normalize_text(text: str) -> str:
    """Normalize input text for fuzzy cache lookup."""
    clean = re.sub(r"[^\w\s]", "", text.lower()).strip()
    return re.sub(r"\s+", " ", clean)


class PromptCache:
    """In-memory cache of pre-rendered industrial audio alerts."""

    def __init__(self, engine: Optional[BaseTTSEngine] = None) -> None:
        self._memory_cache: Dict[str, Tuple[np.ndarray, int]] = {}
        if engine is not None:
            self.generate_all(engine)

    def get(self, text: str) -> Optional[Tuple[np.ndarray, int]]:
        """Look up text in the in-memory prompt cache.

        Returns (audio_data, sample_rate) if found, else None.
        """
        # 1. Direct prompt key lookup (e.g. key == "EMERGENCY_STOP")
        upper_key = text.strip().upper()
        if upper_key in self._memory_cache:
            return self._memory_cache[upper_key]

        # 2. Normalized text and alias matching
        normalized = normalize_text(text)
        canonical_key = PROMPT_ALIASES.get(normalized)
        if canonical_key and canonical_key in self._memory_cache:
            return self._memory_cache[canonical_key]

        # 3. Check against canonical text values directly
        for key, prompt_text in STANDARD_PROMPTS.items():
            if normalize_text(prompt_text) == normalized:
                if key in self._memory_cache:
                    return self._memory_cache[key]

        return None

    def put(self, key: str, audio_data: np.ndarray, sample_rate: int) -> None:
        """Store a pre-rendered prompt into memory."""
        key = key.upper().strip()
        self._memory_cache[key] = (audio_data, sample_rate)

    def generate_all(self, engine: BaseTTSEngine, force: bool = False) -> int:
        """Pre-generate all canonical prompts in-memory using the provided TTS engine."""
        generated = 0
        for key, text in STANDARD_PROMPTS.items():
            if force or key not in self._memory_cache:
                audio_data, rate, _, _ = engine.synthesize(text)
                self.put(key, audio_data, rate)
                generated += 1
        return generated
