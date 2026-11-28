# PCB Vision MVP — Hệ thống Kiểm tra Thiếu / Lỗi Linh kiện PCB

Module Computer Vision kiểm tra bo mạch PCB chạy trực tuyến trên laptop CPU (không cần GPU khi chạy thật), thiết kế theo chuẩn dễ chuyển sang thiết bị nhúng (*embedded-portable*).

dataset: https://universe.roboflow.com/diplom-qz7q6/defects-2q87r

## 1. Kiến trúc Pipeline

```text
Camera UVC (1280×800, fixed exposure/WB)
  │
  ▼
[trigger] Phát hiện bo mạch đứng yên N khung hình (app.py)
  │
  ▼
[align.py] ORB 500 đặc trưng trên ảnh xám nửa phân giải (640×400) + RANSAC Homography
           → Cắt trực tiếp từng slot 64×64 bằng warpPerspective (không warp toàn ảnh)
           → Kiểm tra cổ điển so với ảnh golden (classical_check)
  │
  ▼
[inspector.py] MobileNetV2 width 0.5 (1 batch ONNX Runtime CPU duy nhất cho cả bo)
               + Kết hợp classical_check → PASS / FAIL / FAIL_REVIEW / BOARD_NOT_ALIGNED
  │
  ▼
Overlay trực tuyến (Xanh: Đủ | Đỏ: Thiếu/Lỗi | Vàng: Nghi ngờ) + JSON event PCB_INSPECTION
```

### Quy tắc Portability đã tuân thủ
- **1 file ONNX duy nhất** (`opset 17`, đầu vào cố định `64×64`, dynamic batch), chỉ gồm các toán tử cơ bản: `Conv`, `Clip`, `Add`, `GlobalAveragePool`, `Flatten`, `Gemm`, `Constant`.
- **Chạy thuần CPU** bằng `OpenCV + ONNX Runtime`, không dùng GPU hay thư viện riêng của laptop khi inference.
- **Định mức tốc độ (Headroom):** Toàn pipeline `<= 30 ms` / bo 40 slot trên **1 luồng CPU** (`bench.py` kiểm tra tự động).
- **Cấu hình tập trung:** Toàn bộ camera, ngưỡng, tọa độ slot và đường dẫn model nằm trong 1 file `config.yaml`.

---

## 2. Cấu trúc thư mục & Chức năng từng file

```text
pcb_vision/
├── config.yaml                  # File cấu hình duy nhất (model, camera, trigger, alignment, slots)
├── align.py                     # Căn chỉnh nhanh nửa phân giải (ORB + RANSAC), cắt crop 64×64 & classical check
├── inspector.py                 # Chạy batch ONNX Runtime CPU + hợp nhất classical + xuất JSON PCB_INSPECTION
├── app.py                       # Ứng dụng chạy live trực tuyến từ camera (hoặc 1 ảnh tĩnh) + overlay màu
├── pick_slots.py                # Tool kéo chuột khoanh tọa độ slot trên ảnh golden -> tự lưu vào config.yaml
├── bench.py                     # Đo tốc độ từng bước trên 1 luồng CPU (<= 30 ms) & sai số căn chỉnh (< 2 px)
├── train_mobilenet.ipynb        # Notebook huấn luyện MobileNetV2-0.5, so sánh Trước/Sau và xuất ONNX + INT8 calib
├── requirements.txt             # Danh sách thư viện đã ghim phiên bản (OpenCV, ONNX Runtime, NumPy, PyYAML)
└── exports/                     # Chứa model .onnx, model_meta.json, CSV so sánh và tập ảnh hiệu chuẩn INT8
```

---

## 3. Những gì đã hoàn thành & Kết quả đo thực tế

### Huấn luyện & Xuất ONNX (`mobilenetv2_050_pcb_defects_obb_v1.0`)
- Đã chuyển đổi bộ ảnh YOLO-OBB (`Defects.v16i.yolov8-obb`: `Dry_joint`, `Incorrect_installation`, `Short_circuit`) theo chuẩn **frame-level split** (chống rò rỉ dữ liệu `.rf.` giữa `train / val / heldout`).
- Đã huấn luyện `timm` `mobilenetv2_050` trên GPU, hiệu chỉnh ngưỡng theo từng loại lỗi, xuất ONNX `opset 17` và xác nhận lại độ chính xác trên ONNX Runtime CPU (trùng khớp 100% với PyTorch):

| Chỉ số (Heldout Set) | Trước khi train (Pretrained) | Sau khi train (Fine-tuned) | Sau xuất ONNX (CPU) |
| :--- | :---: | :---: | :---: |
| **Accuracy** | 49.32% | **70.14%** | **70.14%** |
| **Precision** | 45.28% | **63.40%** | **63.40%** |
| **Recall (Defect / Missing)** | 22.43% | **90.65%** | **90.65%** |
| **F1-Score** | 30.00% | **74.62%** | **74.62%** |
| **ROC-AUC** | 47.19% | **77.01%** | **77.01%** |

- **Toán tử ONNX đã kiểm định:** `Add`, `Clip`, `Constant`, `Conv`, `Flatten`, `Gemm`, `GlobalAveragePool` (đạt 100% chuẩn portability).
- **Tập hiệu chuẩn INT8:** Đã tự động lưu sẵn tại `exports/mobilenetv2_050_pcb_defects_obb_v1.0/int8_calibration_crops/`.

### Kiểm định hiệu năng (`bench.py` trên 1 luồng CPU, bo 40 slot)
- **Tổng độ trễ trung bình:** `21.01 ms` (`p95 = 22.29 ms`) — **ĐẠT** cổng `<= 30.0 ms`.
  - Chi tiết: `align = 6.51 ms` | `crop = 1.08 ms` | `classical = 1.87 ms` | `onnx = 11.44 ms` | `decide = 0.09 ms`.
- **Sai số cắt slot tối đa** (xoay `±12°`, lệch `±30 px`): `0.93 px` — **ĐẠT** cổng `<= 2.0 px`.

---

## 4. Hướng dẫn cài đặt & Chạy

Mở terminal tại thư mục `pcb_vision` (đã kích hoạt `.venv`):

### Bước 1 — Cài đặt thư viện
```powershell
pip install -r requirements.txt
```
*(Nếu chạy lại notebook huấn luyện trên máy cá nhân, cần có thêm `timm`, `onnx`, `pandas`, `matplotlib`, `scikit-learn` và `torch` bản CUDA).*

### Bước 2 — (Tùy chọn) Huấn luyện lại model & xuất ONNX
Mở `train_mobilenet.ipynb` → chọn kernel `.venv` → bấm **Restart Kernel → Run All**.
- Model mới sẽ được xuất ra `exports/<model_arch>_<dataset_version>/*.onnx`.
- Cập nhật `model.onnx_path` và `model.version` trong `config.yaml` nếu đổi tên model.

### Bước 3 — Cài đặt bo mẫu chuẩn (làm 1 lần duy nhất cho mỗi loại bo)
Đặt **1 bo mạch chuẩn đầy đủ linh kiện** dưới camera rồi chạy:
```powershell
python pick_slots.py --camera 0 --save-golden assets/golden_pcb.png
```
*(Nếu đã có sẵn ảnh bo chuẩn thì chạy: `python pick_slots.py --image assets/golden_pcb.png`)*
1. Bấm **`Space`** để chụp ảnh golden.
2. Kéo chuột khoanh từng vị trí linh kiện cần kiểm tra (ví dụ `CPU`, `R1`, `C1`...) → bấm **`Enter`** → gõ tên slot trong terminal.
3. Khoanh xong bấm **`Esc`** → tool tự ghi `golden_image_path` và danh sách `slots` vào `config.yaml`.

### Bước 4 — Chạy trực tuyến trên camera / băng chuyền
```powershell
python app.py
```
- Bấm **`q`** hoặc **`Esc`** để thoát.
- Để test nhanh trên 1 ảnh tĩnh (không cần camera):
  ```powershell
  python app.py --image path/to/test_board.png --no-gui --save-overlay out_overlay.png
  ```

## 5. Đầu ra JSON (`PCB_INSPECTION`)

Mỗi lần kiểm tra hoàn tất 1 bo mạch, hệ thống xuất 1 dòng JSON chuẩn:
```json
{
  "event_type": "PCB_INSPECTION",
  "schema_version": "1.0",
  "timestamp_utc": "2026-11-28T09:50:00Z",
  "board_type": "through_hole_kit_v1",
  "model_version": "mobilenetv2_050_pcb_defects_obb_v1.0",
  "result": "FAIL",
  "missing_slots": ["CPU"],
  "review_slots": [],
  "latency_ms": {
    "align": 6.5,
    "crop": 1.1,
    "classical": 1.8,
    "onnx": 11.4,
    "decide": 0.1,
    "total": 20.9
  }
}
```

---

## 6. Lưu ý khi chuyển sang bo thật của team (MVP chính thức)
- Model hiện tại (`pcb_defects_obb_v1.0`) mới huấn luyện trên tập public về lỗi hàn/lắp ráp (`Dry_joint`, `Incorrect_installation`, `Short_circuit`) để kiểm chứng trọn luồng.
- Khi có **bo through-hole kit thật + camera OV9782** trên giá cố định, cần chụp ảnh bo thật (đủ linh kiện và rút từng linh kiện), dùng chính `align.py` cắt crop `64×64` vào `dataset/{train,val,heldout}/{present,missing}/`, rồi chạy lại `train_mobilenet.ipynb` để đạt cổng **`>= 95% missing-slot recall`** trên bo thật.