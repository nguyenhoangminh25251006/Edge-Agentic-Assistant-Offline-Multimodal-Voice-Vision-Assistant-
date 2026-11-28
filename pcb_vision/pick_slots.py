"""Interactive tool to capture a golden PCB image (optional) and draw slot boxes with the mouse.

Usage:
  1. From an existing golden image:
       python pick_slots.py --image assets/golden_pcb.png

  2. Capture golden image directly from webcam first, then draw slots:
       python pick_slots.py --camera 0 --save-golden assets/golden_pcb.png

Controls in the OpenCV window:
  - Drag mouse to draw a box around a component -> press ENTER or SPACE to confirm.
  - Type the slot ID in the terminal (e.g. CPU, R1, C2) and press ENTER.
  - Press ESC (or 'c') without drawing a box to finish and write all slots to config.yaml.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import yaml


def capture_golden_from_camera(device_idx: int, width: int, height: int, save_path: Path) -> Path:
    cap = cv2.VideoCapture(device_idx)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera index {device_idx}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    print("Dat bo chuan ngay ngan duoi camera -> bam SPACE de chup anh golden (ESC de huy).")
    captured = None
    while True:
        ok, frame = cap.read()
        if not ok:
            continue
        vis = frame.copy()
        cv2.putText(vis, "SPACE: Capture golden | ESC: Cancel", (15, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
        cv2.imshow("Capture Golden PCB", vis)
        key = cv2.waitKey(1) & 0xFF
        if key == 32:  # SPACE
            captured = frame.copy()
            break
        if key == 27:  # ESC
            break

    cap.release()
    cv2.destroyAllWindows()
    if captured is None:
        raise SystemExit("Da huy chup anh golden.")

    save_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(save_path), captured)
    print(f"Da luu anh golden tai: {save_path}")
    return save_path


def pick_slots(image_path: Path) -> list[dict]:
    img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {image_path}")

    slots: list[dict] = []
    print("\n--- HUONG DAN ---")
    print("1. Keo chuot khoanh 1 linh kien -> bam ENTER (hoac SPACE) de chot.")
    print("2. Nhap ten slot trong terminal (vi du: CPU, R1, C1...) -> bam ENTER.")
    print("3. Bam ESC (khong khoanh) khi da khoanh xong tat ca slot.\n")

    while True:
        vis = img.copy()
        for s in slots:
            x, y, w, h = s["x"], s["y"], s["w"], s["h"]
            cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 220, 0), 2)
            cv2.putText(vis, s["id"], (x, max(16, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 0), 2)

        roi = cv2.selectROI("Khoanh slot (ENTER=chot, ESC=xong)", vis, fromCenter=False, showCrosshair=True)
        x, y, w, h = map(int, roi)
        if w == 0 or h == 0:
            break

        default_id = f"SLOT{len(slots) + 1}"
        slot_id = input(f"Nhap ten slot (Enter de dung '{default_id}'): ").strip() or default_id
        slots.append({"id": slot_id, "x": x, "y": y, "w": w, "h": h})
        print(f"  + Da them: {{id: '{slot_id}', x: {x}, y: {y}, w: {w}, h: {h}}}")

    cv2.destroyAllWindows()
    return slots


def update_config(cfg_path: Path, golden_path: Path, slots: list[dict]) -> None:
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    try:
        rel_golden = golden_path.resolve().relative_to(cfg_path.resolve().parent).as_posix()
    except ValueError:
        rel_golden = str(golden_path.resolve())
    cfg["alignment"]["golden_image_path"] = rel_golden
    cfg["slots"] = slots
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"\nDa cap nhat {len(slots)} slot va golden_image_path='{rel_golden}' vao {cfg_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive slot picker for PCB golden image")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--image", default=None, help="Existing golden PCB image")
    parser.add_argument("--camera", type=int, default=None, help="Capture golden image from camera index first")
    parser.add_argument("--save-golden", default="assets/golden_pcb.png", help="Where to save captured golden image")
    args = parser.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    if args.camera is not None:
        cam = cfg.get("camera", {})
        golden_path = capture_golden_from_camera(
            args.camera, int(cam.get("width", 1280)), int(cam.get("height", 800)),
            (cfg_path.parent / args.save_golden).resolve(),
        )
    elif args.image is not None:
        golden_path = Path(args.image).resolve()
    else:
        golden_path = (cfg_path.parent / cfg["alignment"]["golden_image_path"]).resolve()

    slots = pick_slots(golden_path)
    if not slots:
        print("Khong co slot nao duoc chon, giu nguyen config.yaml.")
        return
    update_config(cfg_path, golden_path, slots)


if __name__ == "__main__":
    main()

