"""Speech-to-text engine adapters for offline audio transcription."""
import json
import os
from abc import ABC, abstractmethod
from typing import TypedDict

try:
    from audio_stream.preprocessor import enhance_noisy_utterance
    from config import ENABLE_WIENER
except ImportError:
    from ..audio_stream.preprocessor import enhance_noisy_utterance
    from ..config import ENABLE_WIENER


class TranscriptResult(TypedDict):
    text: str
    confidence: float


class BaseSTTEngine(ABC):
    """Abstract base class for speech-to-text engines."""

    @abstractmethod
    def transcribe(self, pcm_bytes: bytes) -> TranscriptResult:
        """Transcribe raw 16 kHz 16-bit mono PCM bytes into text and confidence."""


class VoskSTTEngine(BaseSTTEngine):
    """Vosk speech recognition engine with constrained grammar + [unk] OOV sink."""

    def __init__(self, model_path: str, grammar: list[str], enable_wiener: bool = ENABLE_WIENER) -> None:
        try:
            import vosk  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "Vosk is not installed. Install with: pip install vosk"
            ) from exc

        if not model_path or not os.path.isdir(model_path):
            raise FileNotFoundError(
                f"Vosk model directory not found: '{model_path}'."
            )

        self._enable_wiener = enable_wiener
        self._model = vosk.Model(model_path)

        # Include explicit [unk] token so OOV chatter is absorbed rather than forced into commands
        dedup_grammar = list(dict.fromkeys(grammar))
        if "[unk]" not in dedup_grammar:
            dedup_grammar.append("[unk]")

        grammar_json = json.dumps(dedup_grammar)
        self._recognizer = vosk.KaldiRecognizer(self._model, 16000, grammar_json)
        self._recognizer.SetWords(True)
        print(f"[stt_adapter] Loaded Vosk model from '{model_path}' (grammar={len(dedup_grammar)} entries)")

    def transcribe(self, pcm_bytes: bytes) -> TranscriptResult:
        """Transcribe PCM bytes and return recognized command text and word confidence."""
        if not pcm_bytes:
            return TranscriptResult(text="", confidence=0.0)

        payload = enhance_noisy_utterance(pcm_bytes) if self._enable_wiener else pcm_bytes
        self._recognizer.AcceptWaveform(payload)
        result = json.loads(self._recognizer.FinalResult())

        tokens = [tok for tok in result.get("text", "").split() if tok != "[unk]"]
        text = " ".join(tokens).strip()

        confidence: float = 0.0
        if text:
            words = [w for w in result.get("result", []) if w.get("word") != "[unk]"]
            if words:
                confidence = sum(w.get("conf", 0.95) for w in words) / len(words)
            else:
                confidence = 0.90

        return TranscriptResult(text=text, confidence=confidence)
