"""End-to-end PCB Missing-Component Inspector (ONNX Runtime CPU + classical fusion + JSON event).

Follows all portability rules:
  - Single ONNX model (opset 17, 64x64 NCHW input, dynamic batch).
  - One batched ONNX Runtime inference call per board (all 40 slots at once).
  - CPU-only (optional OpenVINO EP through the exact same ONNX file if enabled).
  - Emits versioned PCB_INSPECTION JSON events.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
import json
import time

import cv2
import numpy as np
import onnxruntime as ort
import yaml

from align import AlignmentResult, FastPCBAligner


IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 1, 1, 3)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 1, 1, 3)


@dataclass
class SlotDecision:
    slot_id: str
    status: str            # PRESENT | MISSING | UNCERTAIN
    p_missing_nn: float
    classical_score: float
    fused_score: float
    threshold: float
    quad_live: np.ndarray  # 4x2 polygon in live frame


@dataclass
class InspectionOutcome:
    result: str            # PASS | FAIL | FAIL_REVIEW | BOARD_NOT_ALIGNED
    missing_slots: List[str]
    review_slots: List[str]
    slot_decisions: List[SlotDecision]
    alignment: AlignmentResult
    latency_ms: Dict[str, float]
    event: dict


class PCBInspector:
    """Runs alignment -> 64x64 slot crops -> batched ONNX classifier + classical check -> decision."""

    def __init__(self, cfg_path: Path | str, ort_threads: Optional[int] = None):
        self.cfg_path = Path(cfg_path).resolve()
        self.base_dir = self.cfg_path.parent
        self.cfg = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))

        self.aligner = FastPCBAligner(self.cfg, base_dir=self.base_dir)
        self.slots = self.cfg["slots"]

        mcfg = self.cfg["model"]
        self.model_version: str = mcfg["version"]
        self.input_size: int = int(mcfg["input_size"])
        onnx_path = self.base_dir / mcfg["onnx_path"]
        if not onnx_path.exists():
            raise FileNotFoundError(f"ONNX model not found: {onnx_path}")

        sess_opts = ort.SessionOptions()
        threads = int(ort_threads if ort_threads is not None else mcfg.get("ort_threads", 1))
        sess_opts.intra_op_num_threads = threads
        sess_opts.inter_op_num_threads = 1
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        providers = ["CPUExecutionProvider"]
        if mcfg.get("use_openvino_if_available", False) and "OpenVINOExecutionProvider" in ort.get_available_providers():
            providers = ["OpenVINOExecutionProvider", "CPUExecutionProvider"]

        self.session = ort.InferenceSession(str(onnx_path), sess_options=sess_opts, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

        dcfg = self.cfg["decision"]
        self.default_thresh = float(dcfg["default_missing_threshold"])
        self.uncertain_band = float(dcfg["uncertain_band"])
        self.classical_weight = float(dcfg.get("classical_weight", 0.15))
        self.per_slot_thresholds: Dict[str, float] = {
            k: float(v) for k, v in (dcfg.get("per_slot_thresholds") or {}).items()
        }

        # Warm up ONNX Runtime once so first live board has stable latency
        dummy = np.zeros((len(self.slots), 3, self.input_size, self.input_size), dtype=np.float32)
        self.session.run([self.output_name], {self.input_name: dummy})

    @staticmethod
    def _preprocess_batch(crops_bgr: List[np.ndarray]) -> np.ndarray:
        """Convert list of (64, 64, 3) BGR uint8 crops to (N, 3, 64, 64) normalized RGB float32."""
        batch = np.stack(crops_bgr, axis=0)[..., ::-1].astype(np.float32) / 255.0  # BGR -> RGB
        batch = (batch - IMAGENET_MEAN) / IMAGENET_STD
        return np.ascontiguousarray(np.transpose(batch, (0, 3, 1, 2)))

    @staticmethod
    def _softmax_missing_prob(logits: np.ndarray) -> np.ndarray:
        """Class 0 = present, Class 1 = missing. Returns P(missing) per slot."""
        shifted = logits - logits.max(axis=1, keepdims=True)
        exps = np.exp(shifted)
        probs = exps / exps.sum(axis=1, keepdims=True)
        return probs[:, 1]

    def inspect(self, frame_bgr: np.ndarray, board_id: Optional[str] = None) -> InspectionOutcome:
        t_start = time.perf_counter()
        align_res = self.aligner.align_and_crop(frame_bgr)

        if not align_res.ok:
            total_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
            lat = {
                "align_ms": round(align_res.align_ms, 2),
                "crop_ms": 0.0,
                "classical_ms": 0.0,
                "onnx_ms": 0.0,
                "decide_ms": 0.0,
                "total_ms": total_ms,
            }
            event = self._build_event(board_id, "BOARD_NOT_ALIGNED", [], [], lat, align_res)
            return InspectionOutcome("BOARD_NOT_ALIGNED", [], [], [], align_res, lat, event)

        # Classical check
        classical_scores, classical_ms = self.aligner.classical_check(align_res.crops_bgr)

        # Batched ONNX Runtime inference (all 40 slots in 1 batch)
        t_nn0 = time.perf_counter()
        batch_nchw = self._preprocess_batch(align_res.crops_bgr)
        logits = self.session.run([self.output_name], {self.input_name: batch_nchw})[0]
        p_missing = self._softmax_missing_prob(logits)
        onnx_ms = (time.perf_counter() - t_nn0) * 1000.0

        # Per-slot & board-level decision
        t_dec0 = time.perf_counter()
        w_cls = self.classical_weight
        slot_decisions: List[SlotDecision] = []
        missing_slots: List[str] = []
        review_slots: List[str] = []

        for i, s in enumerate(self.slots):
            sid = s["id"]
            th = self.per_slot_thresholds.get(sid, self.default_thresh)
            fused = float((1.0 - w_cls) * p_missing[i] + w_cls * classical_scores[i])

            if fused >= th + self.uncertain_band:
                status = "MISSING"
                missing_slots.append(sid)
            elif fused >= th - self.uncertain_band:
                status = "UNCERTAIN"
                review_slots.append(sid)
            else:
                status = "PRESENT"

            slot_decisions.append(
                SlotDecision(
                    slot_id=sid,
                    status=status,
                    p_missing_nn=round(float(p_missing[i]), 4),
                    classical_score=round(float(classical_scores[i]), 4),
                    fused_score=round(fused, 4),
                    threshold=th,
                    quad_live=align_res.live_quads[i],
                )
            )

        if missing_slots:
            board_result = "FAIL"
        elif review_slots:
            board_result = "FAIL_REVIEW"
        else:
            board_result = "PASS"

        decide_ms = (time.perf_counter() - t_dec0) * 1000.0
        total_ms = (time.perf_counter() - t_start) * 1000.0

        lat = {
            "align_ms": round(align_res.align_ms, 2),
            "crop_ms": round(align_res.crop_ms, 2),
            "classical_ms": round(classical_ms, 2),
            "onnx_ms": round(onnx_ms, 2),
            "decide_ms": round(decide_ms, 2),
            "total_ms": round(total_ms, 2),
        }
        event = self._build_event(board_id, board_result, missing_slots, review_slots, lat, align_res)
        return InspectionOutcome(board_result, missing_slots, review_slots, slot_decisions, align_res, lat, event)

    def _build_event(
        self,
        board_id: Optional[str],
        result: str,
        missing_slots: List[str],
        review_slots: List[str],
        latency_ms: Dict[str, float],
        align_res: AlignmentResult,
    ) -> dict:
        ecfg = self.cfg["event"]
        return {
            "event_type": ecfg["event_type"],
            "schema_version": ecfg["schema_version"],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "board_id": board_id or f"board-{int(time.time() * 1000)}",
            "board_type": ecfg["board_type"],
            "result": result,
            "missing_slots": missing_slots,
            "review_slots": review_slots,
            "model_version": self.model_version,
            "alignment": {
                "ok": align_res.ok,
                "reason": align_res.reason,
                "inliers": align_res.inliers,
                "reproj_err_px": round(align_res.reproj_err_px, 3),
            },
            "latency_ms": latency_ms,
        }

    @staticmethod
    def to_json(outcome: InspectionOutcome) -> str:
        return json.dumps(outcome.event, ensure_ascii=False)
