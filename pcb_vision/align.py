"""Fast PCB alignment, per-slot 64x64 cropping (no full-frame warp), and classical baseline check.

Design highlights:
  - Runs ORB (500 features) on a half-resolution greyscale frame (640x400) for speed.
  - Masks out component slot regions on the golden reference image so ORB anchors on
    board edges, fiducials, mounting holes, and silkscreen instead of removable parts.
  - Computes RANSAC homography (golden -> live) and warps ONLY each 64x64 slot crop
    directly from the live frame using one small warpPerspective per slot.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


@dataclass
class AlignmentResult:
    ok: bool
    reason: str
    homography_golden_to_live: Optional[np.ndarray]
    inliers: int
    reproj_err_px: float
    crops_bgr: List[np.ndarray]
    live_quads: List[np.ndarray]   # 4x2 polygon per slot in live frame coords (for overlay)
    align_ms: float
    crop_ms: float


class FastPCBAligner:
    """Aligns live PCB frames to a golden reference and extracts 64x64 slot crops."""

    def __init__(self, cfg: dict, base_dir: Path | str = "."):
        self.base_dir = Path(base_dir)
        acfg = cfg["alignment"]
        self.slots: List[dict] = cfg["slots"]
        self.crop_size: int = int(cfg["model"]["input_size"])
        self.scale: float = float(acfg["scale"])
        self.ratio_test: float = float(acfg["ratio_test"])
        self.ransac_thresh: float = float(acfg["ransac_reproj_thresh_px"])
        self.min_inliers: int = int(acfg["min_inliers"])
        self.max_reproj_err: float = float(acfg["max_reproj_err_px"])
        self.margin_px: int = int(acfg.get("slot_crop_margin_px", 4))
        self.mask_pad_px: int = int(acfg.get("mask_slot_padding_px", 6))

        golden_path = self.base_dir / acfg["golden_image_path"]
        golden_bgr = cv2.imread(str(golden_path), cv2.IMREAD_COLOR)
        if golden_bgr is None:
            raise FileNotFoundError(f"Golden image not found: {golden_path}")
        self.golden_bgr = golden_bgr
        self.golden_h, self.golden_w = golden_bgr.shape[:2]

        # Precompute half-resolution golden greyscale + slot-masked ORB features
        self.orb = cv2.ORB_create(nfeatures=int(acfg["orb_features"]))
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

        golden_half = cv2.resize(
            cv2.cvtColor(golden_bgr, cv2.COLOR_BGR2GRAY),
            (int(self.golden_w * self.scale), int(self.golden_h * self.scale)),
            interpolation=cv2.INTER_AREA,
        )
        orb_mask = self._build_slot_exclusion_mask(golden_half.shape)
        self.kp_golden, self.des_golden = self.orb.detectAndCompute(golden_half, orb_mask)
        if self.des_golden is None or len(self.kp_golden) < self.min_inliers:
            # Fallback without mask if board has very sparse background texture
            self.kp_golden, self.des_golden = self.orb.detectAndCompute(golden_half, None)
        if self.des_golden is None or len(self.kp_golden) < 8:
            raise RuntimeError("Insufficient ORB keypoints on golden reference image.")

        self.pts_golden_half = np.float32([kp.pt for kp in self.kp_golden])

        # Precompute golden slot polygons (in full-res golden coords) and target 64x64 corners
        cs = float(self.crop_size - 1)
        self.dst_crop_pts = np.float32([[0, 0], [cs, 0], [cs, cs], [0, cs]])
        self.slot_src_quads: List[np.ndarray] = []
        self.golden_crops_gray: List[np.ndarray] = []
        self.golden_crop_stats: List[Tuple[np.ndarray, float]] = []

        for s in self.slots:
            x0 = max(0.0, float(s["x"] - self.margin_px))
            y0 = max(0.0, float(s["y"] - self.margin_px))
            x1 = min(float(self.golden_w - 1), float(s["x"] + s["w"] + self.margin_px))
            y1 = min(float(self.golden_h - 1), float(s["y"] + s["h"] + self.margin_px))
            quad = np.float32([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
            self.slot_src_quads.append(quad)

            M = cv2.getPerspectiveTransform(quad, self.dst_crop_pts)
            gcrop = cv2.warpPerspective(golden_bgr, M, (self.crop_size, self.crop_size), flags=cv2.INTER_LINEAR)
            ggray = cv2.cvtColor(gcrop, cv2.COLOR_BGR2GRAY).astype(np.float32)
            # Normalized zero-mean patch for fast NCC + Sobel edge energy
            gmean = ggray - ggray.mean()
            gnorm = float(np.linalg.norm(gmean) + 1e-6)
            gx = cv2.Sobel(ggray, cv2.CV_32F, 1, 0, ksize=3)
            gy = cv2.Sobel(ggray, cv2.CV_32F, 0, 1, ksize=3)
            gedge = float(np.mean(np.abs(gx) + np.abs(gy)) + 1e-6)
            self.golden_crops_gray.append(ggray)
            self.golden_crop_stats.append((gmean / gnorm, gedge))

    def _build_slot_exclusion_mask(self, half_shape: Tuple[int, int]) -> np.ndarray:
        """Mask out component slots on golden image so missing parts do not hurt ORB matching."""
        mask = np.full(half_shape, 255, dtype=np.uint8)
        pad = self.mask_pad_px
        for s in self.slots:
            x0 = max(0, int((s["x"] - pad) * self.scale))
            y0 = max(0, int((s["y"] - pad) * self.scale))
            x1 = min(half_shape[1], int((s["x"] + s["w"] + pad) * self.scale))
            y1 = min(half_shape[0], int((s["y"] + s["h"] + pad) * self.scale))
            mask[y0:y1, x0:x1] = 0
        return mask

    @staticmethod
    def find_board_contour_mask(frame_gray_half: np.ndarray, min_area_ratio: float = 0.15) -> Optional[np.ndarray]:
        """Fast board outline detection via Otsu threshold + largest external contour."""
        h, w = frame_gray_half.shape[:2]
        blur = cv2.GaussianBlur(frame_gray_half, (5, 5), 0)
        _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        largest = max(contours, key=cv2.contourArea)
        if cv2.contourArea(largest) < min_area_ratio * (h * w):
            return None
        mask = np.zeros((h, w), dtype=np.uint8)
        cv2.drawContours(mask, [largest], -1, 255, thickness=cv2.FILLED)
        return mask

    def align_and_crop(self, live_bgr: np.ndarray, use_contour_mask: bool = False) -> AlignmentResult:
        """Align live frame to golden image and warp only 64x64 slot crops (no full-frame warp)."""
        t0 = cv2.getTickCount()
        freq_ms = 1000.0 / cv2.getTickFrequency()

        lh, lw = live_bgr.shape[:2]
        live_half = cv2.resize(
            cv2.cvtColor(live_bgr, cv2.COLOR_BGR2GRAY),
            (int(lw * self.scale), int(lh * self.scale)),
            interpolation=cv2.INTER_AREA,
        )
        live_mask = self.find_board_contour_mask(live_half) if use_contour_mask else None
        kp_live, des_live = self.orb.detectAndCompute(live_half, live_mask)

        if des_live is None or len(kp_live) < self.min_inliers:
            t1 = cv2.getTickCount()
            return AlignmentResult(False, "insufficient_live_features", None, 0, 999.0, [], [], (t1 - t0) * freq_ms, 0.0)

        knn = self.matcher.knnMatch(self.des_golden, des_live, k=2)
        good = []
        for pair in knn:
            if len(pair) == 2 and pair[0].distance < self.ratio_test * pair[1].distance:
                good.append(pair[0])

        if len(good) < self.min_inliers:
            t1 = cv2.getTickCount()
            return AlignmentResult(False, f"too_few_matches({len(good)})", None, len(good), 999.0, [], [], (t1 - t0) * freq_ms, 0.0)

        src_pts = np.float32([self.pts_golden_half[m.queryIdx] for m in good]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp_live[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

        H_half, inlier_mask = cv2.findHomography(
            src_pts, dst_pts, cv2.RANSAC, self.ransac_thresh * self.scale
        )
        if H_half is None or inlier_mask is None:
            t1 = cv2.getTickCount()
            return AlignmentResult(False, "homography_failed", None, 0, 999.0, [], [], (t1 - t0) * freq_ms, 0.0)

        inliers_bool = inlier_mask.ravel().astype(bool)
        n_inliers = int(inliers_bool.sum())
        if n_inliers < self.min_inliers:
            t1 = cv2.getTickCount()
            return AlignmentResult(False, f"too_few_inliers({n_inliers})", None, n_inliers, 999.0, [], [], (t1 - t0) * freq_ms, 0.0)

        # Compute full-resolution reprojection error on inliers
        proj_half = cv2.perspectiveTransform(src_pts[inliers_bool], H_half)
        err_px = float(np.mean(np.linalg.norm(proj_half - dst_pts[inliers_bool], axis=-1)) / self.scale)
        if err_px > self.max_reproj_err:
            t1 = cv2.getTickCount()
            return AlignmentResult(False, f"high_reproj_err({err_px:.2f}px)", None, n_inliers, err_px, [], [], (t1 - t0) * freq_ms, 0.0)

        # Scale homography from half-res to full-res: H_full = S^-1 * H_half * S
        inv_s = 1.0 / self.scale
        S = np.diag([self.scale, self.scale, 1.0]).astype(np.float64)
        S_inv = np.diag([inv_s, inv_s, 1.0]).astype(np.float64)
        H_full = S_inv @ H_half @ S

        t1 = cv2.getTickCount()
        align_ms = (t1 - t0) * freq_ms

        # Warp ONLY each 64x64 slot crop directly from the live frame (no full-frame warp)
        crops_bgr: List[np.ndarray] = []
        live_quads: List[np.ndarray] = []
        for quad_g in self.slot_src_quads:
            quad_live = cv2.perspectiveTransform(quad_g.reshape(-1, 1, 2), H_full).reshape(4, 2).astype(np.float32)
            live_quads.append(quad_live)
            M_slot = cv2.getPerspectiveTransform(quad_live, self.dst_crop_pts)
            crop = cv2.warpPerspective(live_bgr, M_slot, (self.crop_size, self.crop_size), flags=cv2.INTER_LINEAR)
            crops_bgr.append(crop)

        t2 = cv2.getTickCount()
        crop_ms = (t2 - t1) * freq_ms
        return AlignmentResult(True, "ok", H_full, n_inliers, err_px, crops_bgr, live_quads, align_ms, crop_ms)

    def classical_check(self, live_crops_bgr: List[np.ndarray]) -> Tuple[np.ndarray, float]:
        """Fast classical baseline returning per-slot missing_score in [0, 1] (higher = more likely missing).

        Combines:
          1) Normalized Cross-Correlation drop vs. golden slot crop (robust to uniform brightness shifts)
          2) Sobel edge-energy drop vs. golden slot crop (missing components have fewer strong edges)
        """
        t0 = cv2.getTickCount()
        scores = np.zeros(len(live_crops_bgr), dtype=np.float32)
        for i, crop in enumerate(live_crops_bgr):
            g_unit, g_edge = self.golden_crop_stats[i]
            lgray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32)
            lmean = lgray - lgray.mean()
            lnorm = float(np.linalg.norm(lmean) + 1e-6)
            ncc = float(np.clip(np.sum((lmean / lnorm) * g_unit), -1.0, 1.0))
            ncc_diff = 0.5 * (1.0 - ncc)  # 0 = identical, 1 = inverted

            lx = cv2.Sobel(lgray, cv2.CV_32F, 1, 0, ksize=3)
            ly = cv2.Sobel(lgray, cv2.CV_32F, 0, 1, ksize=3)
            ledge = float(np.mean(np.abs(lx) + np.abs(ly)))
            edge_drop = float(np.clip(1.0 - ledge / g_edge, 0.0, 1.0))

            scores[i] = float(np.clip(0.65 * ncc_diff + 0.35 * edge_drop, 0.0, 1.0))

        elapsed_ms = (cv2.getTickCount() - t0) * 1000.0 / cv2.getTickFrequency()
        return scores, elapsed_ms
