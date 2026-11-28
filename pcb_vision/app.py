"""Laptop MVP PCB Missing-Component Inspector App.

Supports:
  1. Live UVC camera mode (OV9782 at 1280x800, fixed exposure & white balance) with stability trigger.
  2. Single-image / folder test mode (--image path/to/board.png) for offline verification.
  3. Live overlay (green = present, red = missing, amber = uncertain) + per-stage latency HUD.
  4. Versioned PCB_INSPECTION JSON event output (stdout + optional JSONL log file).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import time
from typing import Optional

import cv2
import numpy as np

from inspector import InspectionOutcome, PCBInspector


COLOR_MAP = {
    "PRESENT": (40, 200, 40),      # Green (BGR)
    "MISSING": (30, 30, 235),      # Red
    "UNCERTAIN": (0, 185, 255),    # Amber
}
BANNER_COLOR = {
    "PASS": (35, 165, 35),
    "FAIL": (30, 30, 220),
    "FAIL_REVIEW": (0, 165, 240),
    "BOARD_NOT_ALIGNED": (90, 90, 90),
}


class StabilityTrigger:
    """Triggers inspection once a board is stationary in the frame for N consecutive frames."""

    def __init__(self, stable_frames: int = 5, motion_thresh: float = 4.0, cooldown_s: float = 1.5):
        self.stable_frames = stable_frames
        self.motion_thresh = motion_thresh
        self.cooldown_s = cooldown_s
        self._prev_small: Optional[np.ndarray] = None
        self._stable_count = 0
        self._last_fire = 0.0

    def update(self, frame_bgr: np.ndarray) -> bool:
        small = cv2.resize(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY), (160, 100), interpolation=cv2.INTER_AREA)
        if self._prev_small is None:
            self._prev_small = small
            return False
        diff = float(np.mean(np.abs(small.astype(np.float32) - self._prev_small.astype(np.float32))))
        self._prev_small = small

        if diff <= self.motion_thresh:
            self._stable_count += 1
        else:
            self._stable_count = 0

        now = time.monotonic()
        if self._stable_count >= self.stable_frames and (now - self._last_fire) >= self.cooldown_s:
            self._last_fire = now
            self._stable_count = 0
            return True
        return False


def draw_overlay(frame_bgr: np.ndarray, outcome: InspectionOutcome) -> np.ndarray:
    """Draw slot polygons (green/red/amber), board verdict banner, and per-stage latency HUD."""
    vis = frame_bgr.copy()

    for sd in outcome.slot_decisions:
        color = COLOR_MAP.get(sd.status, (200, 200, 200))
        pts = sd.quad_live.astype(np.int32).reshape((-1, 1, 2))
        thickness = 2 if sd.status == "PRESENT" else 3
        cv2.polylines(vis, [pts], isClosed=True, color=color, thickness=thickness)
        x_txt, y_txt = int(sd.quad_live[0, 0]), max(16, int(sd.quad_live[0, 1]) - 4)
        label = sd.slot_id if sd.status == "PRESENT" else f"{sd.slot_id} ({sd.fused_score:.2f})"
        cv2.putText(vis, label, (x_txt, y_txt), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1, cv2.LINE_AA)

    # Top verdict banner
    bcol = BANNER_COLOR.get(outcome.result, (80, 80, 80))
    cv2.rectangle(vis, (0, 0), (vis.shape[1], 54), (20, 20, 20), thickness=cv2.FILLED)
    cv2.rectangle(vis, (0, 0), (260, 54), bcol, thickness=cv2.FILLED)
    cv2.putText(vis, outcome.result, (14, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2, cv2.LINE_AA)

    lat = outcome.latency_ms
    hud_line1 = (
        f"Total: {lat['total_ms']:.1f}ms | Align: {lat['align_ms']:.1f}ms | Crop: {lat['crop_ms']:.1f}ms | "
        f"Classical: {lat['classical_ms']:.1f}ms | ONNX: {lat['onnx_ms']:.1f}ms"
    )
    missing_txt = ", ".join(outcome.missing_slots[:10]) if outcome.missing_slots else "None"
    hud_line2 = f"Missing ({len(outcome.missing_slots)}): {missing_txt} | Review: {len(outcome.review_slots)}"
    cv2.putText(vis, hud_line1, (275, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (230, 230, 230), 1, cv2.LINE_AA)
    cv2.putText(vis, hud_line2, (275, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (180, 220, 255), 1, cv2.LINE_AA)
    return vis


def open_uvc_camera(cam_cfg: dict) -> cv2.VideoCapture:
    """Open OV9782 UVC camera at 1280x800 with fixed exposure and white balance."""
    cap = cv2.VideoCapture(int(cam_cfg["device_index"]))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open UVC camera index {cam_cfg['device_index']}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(cam_cfg["width"]))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(cam_cfg["height"]))
    cap.set(cv2.CAP_PROP_FPS, int(cam_cfg["fps"]))
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, float(cam_cfg["auto_exposure"]))
    cap.set(cv2.CAP_PROP_EXPOSURE, float(cam_cfg["exposure"]))
    cap.set(cv2.CAP_PROP_AUTO_WB, float(cam_cfg["auto_wb"]))
    cap.set(cv2.CAP_PROP_WB_TEMPERATURE, float(cam_cfg["wb_temperature"]))
    return cap


def publish_event(outcome: InspectionOutcome, event_log: Optional[Path] = None):
    payload = PCBInspector.to_json(outcome)
    print(payload, flush=True)
    if event_log is not None:
        event_log.parent.mkdir(parents=True, exist_ok=True)
        with event_log.open("a", encoding="utf-8") as f:
            f.write(payload + "\n")


def main():
    parser = argparse.ArgumentParser(description="PCB Missing-Component Inspector (Laptop MVP)")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config file")
    parser.add_argument("--image", default=None, help="Inspect a single image instead of live camera")
    parser.add_argument("--save-overlay", default=None, help="Optional path to save overlay PNG in --image mode")
    parser.add_argument("--event-log", default=None, help="Optional JSONL file to append PCB_INSPECTION events")
    parser.add_argument("--threads", type=int, default=None, help="Override ONNX Runtime CPU threads")
    parser.add_argument("--no-gui", action="store_true", help="Disable OpenCV preview window")
    args = parser.parse_args()

    inspector = PCBInspector(args.config, ort_threads=args.threads)
    event_log = Path(args.event_log) if args.event_log else None

    if args.image:
        frame = cv2.imread(args.image, cv2.IMREAD_COLOR)
        if frame is None:
            raise FileNotFoundError(f"Cannot read image: {args.image}")
        outcome = inspector.inspect(frame, board_id=Path(args.image).stem)
        publish_event(outcome, event_log)
        vis = draw_overlay(frame, outcome)
        if args.save_overlay:
            cv2.imwrite(args.save_overlay, vis)
        if not args.no_gui:
            cv2.imshow("PCB Inspector MVP", vis)
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        return

    tcfg = inspector.cfg["trigger"]
    trigger = StabilityTrigger(
        stable_frames=int(tcfg["stable_frames"]),
        motion_thresh=float(tcfg["motion_diff_threshold"]),
        cooldown_s=float(tcfg["cooldown_s"]),
    )
    cap = open_uvc_camera(inspector.cfg["camera"])
    last_vis: Optional[np.ndarray] = None
    print("Live camera started. Press 'q' to quit, 'Space' to force inspect.", flush=True)
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.02)
                continue
            key = cv2.waitKey(1) & 0xFF if not args.no_gui else 255
            if key == ord("q"):
                break
            if key == ord(" ") or trigger.update(frame):
                outcome = inspector.inspect(frame)
                publish_event(outcome, event_log)
                last_vis = draw_overlay(frame, outcome)
            if not args.no_gui:
                cv2.imshow("PCB Inspector MVP", last_vis if last_vis is not None else frame)
    finally:
        cap.release()
        if not args.no_gui:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
