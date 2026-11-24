"""TTS Voice Pipeline Coordinator.

Modes:
  1. PRODUCTION DAEMON (Default: `python main.py`):
     - Ingests TTSRequest events from container-orchestrator over ZeroMQ PULL socket.
     - Dynamically synthesizes speech via Piper (en_US-lessac-low ONNX Runtime CPU) in-memory.
     - Manages priority playback queue with immediate emergency stop preemption.
     - Plays directly to physical speakers (sounddevice / ALSA) without saving WAV files to disk.

  2. TEST FROM FILE MODE (`python main.py --test-from-file [file]`):
     - Discovers sample text files in `test_input/`.
     - Allows interactive selection (or explicit file argument).
     - Reads text lines and outputs speech aloud in real time.

Target Edge Platform: Arduino UNO Q (Qualcomm QRB2210 MPU + STM32U585 MCU)
"""
import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

try:
    from audio_playback.player import AudioPlayer
    from audio_playback.queue_manager import AudioQueueManager
    from config import (
        PIPELINE_DIR,
        TEST_INPUT_DIR,
        ZMQ_PUB_ENDPOINT,
        ZMQ_PULL_ENDPOINT,
        get_piper_config_path,
        get_piper_model_path,
    )
    from schemas import PriorityEnum, TTSRequest, TTSStatusEnum, TTSStatusEvent
    from tts_engine import PiperEngine, PromptCache
    from zmq_listener import ZMQListener
except ImportError:
    from .audio_playback.player import AudioPlayer
    from .audio_playback.queue_manager import AudioQueueManager
    from .config import (
        PIPELINE_DIR,
        TEST_INPUT_DIR,
        ZMQ_PUB_ENDPOINT,
        ZMQ_PULL_ENDPOINT,
        get_piper_config_path,
        get_piper_model_path,
    )
    from .schemas import PriorityEnum, TTSRequest, TTSStatusEnum, TTSStatusEvent
    from .tts_engine import PiperEngine, PromptCache
    from .zmq_listener import ZMQListener


def load_prompts(prompt_path: Path) -> List[str]:
    """Read and filter non-empty lines from a text prompt file."""
    if not prompt_path.is_file():
        raise FileNotFoundError(f"Test input file not found: '{prompt_path}'")

    prompts: List[str] = []
    with open(prompt_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                prompts.append(stripped)
    return prompts


def select_test_file(test_input_dir: Path) -> Optional[Path]:
    """Interactively list and select a test text file from test_input/."""
    if not test_input_dir.is_dir():
        print(f"[main] Error: Directory '{test_input_dir}' not found.")
        return None

    files = sorted(list(test_input_dir.glob("*.txt")))
    if not files:
        print(f"[main] Error: No .txt files found in '{test_input_dir}'.")
        return None

    print("\n" + "=" * 70)
    print("EDGE AGENTIC ASSISTANT - SELECT TEST INPUT FILE")
    print(f"Directory: {test_input_dir}")
    print("=" * 70)
    for idx, fpath in enumerate(files, start=1):
        desc = ""
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                if first_line.startswith("#"):
                    desc = f" ({first_line.lstrip('#').strip()})"
        except Exception:
            pass
        print(f"  [{idx}] {fpath.name:<28}{desc}")
    print("-" * 70)

    while True:
        try:
            choice = input(f"Enter file number (1-{len(files)}) or 'q' to cancel: ").strip()
        except EOFError:
            print(f"\n[main] Non-interactive environment detected. Using file [1]: {files[0].name}")
            return files[0]

        if choice.lower() in ("q", "quit", "exit"):
            return None

        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(files):
                return files[idx - 1]
            print(f"Invalid selection. Please enter a number between 1 and {len(files)}.")
        else:
            match = [f for f in files if f.name.lower() == choice.lower()]
            if match:
                return match[0]
            print(f"File '{choice}' not found. Please try again.")


def run_file_test_mode(
    test_file_path: Path,
    piper: PiperEngine,
    player: AudioPlayer,
) -> None:
    """Read prompts from chosen text file and output speech through speakers."""
    prompts = load_prompts(test_file_path)
    if not prompts:
        print(f"[main] Warning: No active text lines found in '{test_file_path}'.")
        return

    print("\n" + "=" * 70)
    print(f"[main] Speaking prompts from: {test_file_path.name} ({len(prompts)} items)")
    print("=" * 70)

    for idx, text in enumerate(prompts, start=1):
        t0 = time.perf_counter()
        audio_data, sample_rate, synth_time_s, rtf = piper.synthesize(text)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        duration_s = len(audio_data) / sample_rate if sample_rate > 0 else 0.0

        print("-" * 70)
        print(f"  [PROMPT {idx}/{len(prompts)}]")
        print(f"  Text       : \"{text}\"")
        print(f"  Latency    : {latency_ms:.1f} ms | Audio Len: {duration_s:.2f} s | RTF: {rtf:.3f}")
        print(f"  Playback   : Speaking through audio output...")

        # Play audio directly in-memory through physical speakers (no WAV files saved)
        player.play(audio_data, sample_rate=sample_rate, blocking=True)
        print("  Playback   : Finished.")

        if idx < len(prompts):
            time.sleep(0.3)

    print("-" * 70)
    print(f"\n[main] Test file '{test_file_path.name}' finished successfully.\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Edge Agentic Assistant - TTS Voice Pipeline"
    )
    parser.add_argument(
        "--test-from-file",
        nargs="?",
        const="PROMPT_USER",
        default=None,
        metavar="FILE",
        help="Read from sample text file and speak. If no file given, shows selection menu.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without physical audio hardware (simulates playback timing)",
    )
    parser.add_argument(
        "--volume",
        type=float,
        default=1.0,
        help="Playback volume multiplier between 0.0 and 1.0 (default: 1.0)",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("EDGE AGENTIC ASSISTANT - TTS VOICE PIPELINE")
    print("Target Platform: Arduino UNO Q (Qualcomm QRB2210 + STM32U585)")
    print("Voice Model    : Piper en_US-lessac-low (ONNX Runtime CPU)")
    if args.test_from_file:
        print("Engine Mode    : TEST FROM FILE")
    else:
        print("Engine Mode    : PRODUCTION (ZeroMQ Microservice Daemon)")
    print(f"Audio Output   : {'Simulation (Headless)' if args.headless else 'Physical Speakers / sounddevice'}")
    print("=" * 70)

    # Initialize Piper neural engine and audio player
    piper = PiperEngine(
        model_path=get_piper_model_path(),
        config_path=get_piper_config_path(),
    )
    player = AudioPlayer(headless=args.headless, default_volume=args.volume)

    # ---------------------------------------------------------
    # MODE 1: TEST FROM FILE MODE
    # ---------------------------------------------------------
    if args.test_from_file is not None:
        target_file: Optional[Path] = None

        if args.test_from_file == "PROMPT_USER":
            # Interactive selection menu
            target_file = select_test_file(TEST_INPUT_DIR)
            if target_file is None:
                print("[main] No file selected. Exiting.")
                sys.exit(0)
        else:
            # File specified on command line
            candidate = Path(args.test_from_file)
            if candidate.is_file():
                target_file = candidate
            elif (TEST_INPUT_DIR / args.test_from_file).is_file():
                target_file = TEST_INPUT_DIR / args.test_from_file
            elif (TEST_INPUT_DIR / f"{args.test_from_file}.txt").is_file():
                target_file = TEST_INPUT_DIR / f"{args.test_from_file}.txt"
            else:
                print(f"[main] Error: Cannot find test input file '{args.test_from_file}' in '{TEST_INPUT_DIR}'.")
                sys.exit(1)

        try:
            run_file_test_mode(target_file, piper, player)
        finally:
            player.stop()
            print("[main] Test playback closed.")
        return

    # ---------------------------------------------------------
    # MODE 2: PRODUCTION DAEMON SERVICE (Default)
    # ---------------------------------------------------------
    cache = PromptCache(engine=piper)
    queue_mgr = AudioQueueManager(player=player)
    queue_mgr.start()

    listener = ZMQListener(
        pull_endpoint=ZMQ_PULL_ENDPOINT,
        pub_endpoint=ZMQ_PUB_ENDPOINT,
        bind_pull=True,
    )
    queue_mgr.status_callback = listener.publish_status

    print(f"[main] TTS Daemon active.")
    print(f"       ZeroMQ Ingest (PULL): {listener.pull_endpoint}")
    print(f"       ZeroMQ Status (PUB) : {listener.pub_endpoint}")
    print("       Awaiting TTSRequest payloads from orchestrator (Ctrl+C to stop)...\n")

    try:
        while True:
            request = listener.recv_request(timeout_ms=500)
            if request is not None:
                # 1. Check in-memory prompt cache
                hit = cache.get(request.text)
                if hit is not None:
                    audio_data, sample_rate = hit
                    cached = True
                    synth_time = 0.0
                elif request.use_cache_only:
                    listener.publish_status(
                        TTSStatusEvent(
                            request_id=request.request_id,
                            status=TTSStatusEnum.FAILED,
                            cached=False,
                            error_msg="Prompt not found in cache and use_cache_only=True",
                        )
                    )
                    continue
                else:
                    audio_data, sample_rate, synth_time, _ = piper.synthesize(request.text)
                    cached = False

                queue_mgr.enqueue(
                    request=request,
                    audio_data=audio_data,
                    sample_rate=sample_rate,
                    cached=cached,
                    synthesis_time_s=synth_time,
                )
    except KeyboardInterrupt:
        print("\n[main] Shutting down TTS daemon...")
    finally:
        queue_mgr.stop()
        listener.close()
        print("[main] TTS service stopped.")


if __name__ == "__main__":
    main()
