# Wake word "Hey Edgie" trên MCU — pipeline, model, benchmark trên Pico 2

Ngày: 26/11/2026 · Nhánh `embedded` · Phần cứng đo: Raspberry Pi Pico 2 (RP2350, Cortex-M33 150 MHz, 520 KB SRAM, 4 MB QSPI flash), **chưa có micro** → audio thử được nhúng vào flash.
Mọi con số dưới đây là **số đo thật** trên Pico 2 hoặc trên PC (ghi rõ chỗ nào là PC). Không có số nào của UNO Q: chưa có board, không suy ra từ Pico (lý do ở §6).

## 1. Tóm tắt

| Hạng mục | Kết quả |
|---|---|
| Cụm từ | **"Hey Edgie"** (/heɪ ˈɛdʒi/, 3 âm tiết, từ chữ *Edge* của dự án) |
| Kiến trúc | Wake word trên **MCU**, STT/SLM trên **MPU** — MCU nghe liên tục, Linux chỉ thức dậy sau `VOICE_EVENT WAKE` |
| Model chốt | **DS-CNN-S int8** — 23.747 tham số, 2,66 M MAC, 44,9 KB flatbuffer, arena 23 KB |
| Chạy trên Pico 2 | MFCC **0,44–0,55 ms / 20 ms**; inference **65 ms** (weights trong SRAM); chạy mỗi **100 ms** chỉ khi có tiếng nói |
| Độ trễ wake (từ lúc nói xong "Hey Edgie" tới khi MCU gửi WAKE) | **p50 80 ms · p90 233 ms** (trước: 222 / 336 ms) — ưu tiên số 1 của pipeline, §4.4 |
| Tải CPU | **2,7 %** khi im lặng (chỉ MFCC + VAD) · **~68 %** khi có người nói (vẫn dưới các luồng an toàn) |
| Port đúng | Output int8 trên MCU **trùng từng bit** với TFLite trên PC: **14/14** clip |
| An toàn embedded | HIL **30/30** khi model chạy liên tục (VAD tắt = tải xấu nhất): jitter vòng điều khiển 100 Hz **max 207 µs** (NFR-01 ≤ 1 ms), heartbeat timeout phát hiện sau 328 ms, telemetry 19,8 Hz |
| Độ chính xác (giọng chưa thấy khi train) | per-clip: đúng **97,8 %**, bỏ sót 9,4 % · streaming 70 phút: bỏ sót **9,0 %**, báo nhầm **19,8 lần/giờ nói liên tục** (xem §4.3 — đây là điều kiện xấu nhất) · phòng yên tĩnh 30 phút: **0** báo nhầm |
| Giới hạn lớn nhất | Model **chỉ train bằng giọng tổng hợp (TTS)**. Trước khi lên rig phải thu giọng thật của team (§7) |

## 2. Pipeline đề xuất (MCU luôn bật, Linux ngủ)

```
 INMP441 ──I2S/PIO──► [0] ring buffer 16 kHz int16 (SRAM)
                         │ mỗi 20 ms (320 mẫu)
                         ▼
                     [1] MFCC 49×10 + năng lượng frame            0,5 ms  ← luôn chạy, 2,7 % CPU
                         │
                         ▼
                     [2] VAD năng lượng (noise floor tự bám,      ~0 ms
                         ngưỡng +9 dB, giữ 1 s sau tiếng nói)
                         │ im lặng → dừng ở đây (model không chạy)
                         ▼ có tiếng nói
                     [3] DS-CNN-S int8 trên cửa sổ 1 s           65 ms, mỗi 100 ms
                         (TFLite Micro + CMSIS-NN)               ← chỉ khi có tiếng nói
                         ▼
                     [4] trung bình 2 điểm gần nhất ≥ 95 %,
                         rồi khoá 1,5 s (refractory)
                         ▼
   MCU: LED nghe GP15, cờ VOICE_ACTIVE, VOICE_EVENT WAKE (0x93, gửi lại tới khi EVENT_ACK)
        + ≤ 5 s audio lệnh qua USB CDC (32 kB/s)
                         ▼
   MPU (Pi 3B / UNO Q Linux): Vosk STT thức dậy → grammar = tầng xác minh thứ 2
        (báo nhầm của MCU mà không ra lệnh hợp lệ thì bị bỏ) → SLM → orchestrator
                         ▼
   MCU kiểm tra lại mọi lệnh (state, giới hạn tốc độ, TTL, cmd_id) — "MPU proposes, MCU decides"
```

Vì sao chia như vậy:
* **Tầng rẻ chạy liên tục, tầng đắt chỉ chạy khi cần.** MFCC + VAD tốn 2,7 % CPU. Model 65 ms chỉ chạy khi có tiếng nói. MPU không phải nghe liên tục. Trước đây STT idle trên MPU ước ~2,5 % tải (CH-204); giờ còn 0 cho tới khi có wake.
* **Báo nhầm rẻ và an toàn.** Một lần wake nhầm chỉ làm STT thức 5 s. Không lệnh nào tới motor nếu STT/SLM không ra lệnh hợp lệ, và MCU vẫn kiểm tra lại lệnh. Bỏ sót thì người nói lại.
* **Dừng khẩn không phụ thuộc wake word.** E-stop phần cứng luôn thắng. Class "stop" nói ra **chưa có trong model này** (theo yêu cầu: chỉ trigger wake). Đường code `KWS_STOP` trong `voice.c` vẫn còn, khi team muốn thì thêm class (Speech Commands có sẵn ~3.800 mẫu "stop" giọng thật).

## 3. Model và front-end

### 3.1 Front-end (một file C cho cả train lẫn MCU)
`firmware/src/voice/kws_frontend.c`:
* Frame 30 ms, hop 20 ms, Hann, FFT 512.
* 40 mel 20–8000 Hz, log, DCT → **10 MFCC × 49 frame** = cửa sổ 1 s.

Cùng file đó được build thành DLL cho Python (`kws/fe.py`), nên model được train trên **đúng** đặc trưng MCU tính. Kiểm tra với numpy: sai số tối đa 1,9·10⁻⁶.

### 3.2 Hai kiến trúc thử
| | DS-CNN-S | DS-CNN-XS |
|---|---|---|
| Cấu trúc | conv 10×4/2, 64 kênh + 4 khối DW-sep | conv 10×4/2, 32 kênh + 3 khối |
| Tham số / MAC | 23.747 / 2.656.192 | 6.211 / 652.096 |
| Flatbuffer int8 / arena | 44.880 B / 22.964 B | 17.720 B / 11.812 B |
| Thời gian train (CPU i5-7500T) | ~43 phút | ~5 phút |

Lượng tử hoá full-int8 (input/output int8), batch cố định 1. Batch động sinh op `SHAPE/PACK/STRIDED_SLICE` mà TFLM không có: đã gặp và sửa. `train.py` giờ chặn build nếu model dùng op chưa đăng ký.

### 3.3 Dữ liệu
| Tập | wake | không phải wake |
|---|---|---|
| train | 6.172 | 22.998 |
| val (người nói tách riêng) | 778 | 2.752 |
| test (**giọng/engine không có trong train**) | 167 | 2.452 |

**Nguồn dữ liệu:**
* **wake:**
  * Piper TTS: LibriTTS-R 904 người, VCTK 109 người, L2-ARCTIC 24 người non-native (có người Việt), ARCTIC, cùng vài giọng đơn.
  * Mỗi mẫu được random tốc độ và ngữ điệu.
* **không phải wake:**
  * Google Speech Commands v2: 35 từ, giọng người thật, tách theo người nói.
  * Từ dễ nhầm: "hey Eddie", "edgy", "hey Reggie", "hedge", "hey Siri"…
  * Câu trong xưởng: "start the belt", "stop", "speed up"…
* **test:**
  * 4 giọng Piper không dùng khi train.
  * 2 giọng Windows SAPI (David, Zira).
* **Augmentation:**
  * Dịch thời gian ±150 ms.
  * Vang (RT60 0,15–0,6 s).
  * Mức nói −26…+4 dB.
  * Ồn SNR 0–25 dB: tiếng nền của Speech Commands, motor 50/100/150 Hz kèm tiếng rít PWM, brown noise.
  * Ồn nội của mic.

## 4. Kết quả độ chính xác (PC, model int8 — cùng output với MCU)

### 4.1 Per-clip, ngưỡng 0,8, tập test giọng chưa thấy
| Model | Dữ liệu | Đúng | Bỏ sót | Báo nhầm / clip |
|---|---|---|---|---|
| XS | v1 (chỉ LibriTTS) | 92,4 % | 39,2 % | 0,44 % |
| XS | v2 (+VCTK, L2-ARCTIC, ARCTIC…) | 95,5 % | 22,5 % | 0,58 % |
| S | v1 | 95,8 % | 23,5 % | 0,27 % |
| **S** | **v2** | **97,8 %** | **9,4 %** | 0,58 % |

Thêm đa dạng giọng giảm bỏ sót **2,5 lần** với S. Đây là bằng chứng rằng giới hạn hiện tại là **dữ liệu**, không phải model.

### 4.2 Chênh lệch theo giọng (XS v2, cùng tập test)
Điểm wake trung bình khi có ồn: northern_english_male 0,90 · lessac 0,85 · amy 0,95 · **alan 0,52** · SAPI Zira 0,99 · SAPI David 0,86.
Có giọng gần như không được nhận. Với người thật cũng sẽ có chênh lệch như vậy, nên §7 là bắt buộc.

### 4.3 Streaming (copy y hệt `kws_pipeline.c` bằng Python — `eval_stream.py`)
**Cách dựng luồng thử:** 70 phút nói **liên tục**, ghép từ tập test (phần lớn là từ dễ nhầm và lệnh), xen 167 lần "Hey Edgie", nền ồn xưởng.

| Model, chu kỳ, làm mượt | ngưỡng | bỏ sót | báo nhầm / giờ |
|---|---|---|---|
| XS, 60 ms, 3 | 80 % | 20,4 % | 23,3 |
| S, 100 ms, 3 | 80 % | 8,4 % | 16,4 |
| S, 200 ms, 2 (chốt cũ) | 80 % | 10,8 % | 19,8 |
| S, 200 ms, 2 | 90 % | 13,8 % | 13,8 |
| **S, 100 ms, 2 (chốt mới, §4.4)** | **95 %** | **9,0 %** | **19,8** |

**Phòng yên tĩnh** (30 phút, ~10 % thời gian có tiếng nói): 0 báo nhầm, model chạy ở 49 % số lần đến lượt (còn lại VAD bỏ qua).

Đọc số "19,8 lần/giờ" cho đúng:
* Đây là 1 giờ **toàn tiếng nói**, phần lớn là "hey Eddie", "edgy"…, tức điều kiện xấu nhất có chủ đích.
* Xưởng thật có ít tiếng nói hơn nhiều, nên con số thực tế sẽ thấp hơn. **Nhưng chưa đo với người thật nên không đưa ra con số thay thế.**
* Một báo nhầm chỉ làm STT thức dậy (§2), không làm motor chạy.

### 4.4 Độ trễ wake — tham số phải nhanh nhất
Độ trễ = từ lúc kết thúc cụm "Hey Edgie" (đo trên clip sạch) tới lúc MCU gửi `VOICE_EVENT WAKE`, đã cộng thời gian inference đo trên Pico (S 65 ms, XS 24 ms). Cùng luồng thử 70 phút như §4.3.

| Model, chu kỳ, làm mượt | ngưỡng | bỏ sót | báo nhầm / giờ | trễ p50 | trễ p90 | trễ max |
|---|---|---|---|---|---|---|
| S, 200 ms, 2 (chốt cũ) | 80 % | 10,8 % | 19,8 | 222 ms | 336 ms | 648 ms |
| S, 100 ms, 3 | 80 % | 8,4 % | 16,4 | 161 ms | 260 ms | 473 ms |
| S, 100 ms, 2 | 80 % | 6,6 % | 27,6 | 72 ms | 200 ms | 494 ms |
| **S, 100 ms, 2 (chốt)** | **95 %** | **9,0 %** | **19,8** | **80 ms** | **233 ms** | **494 ms** |
| S, 100 ms, 1 | 80 % | 2,4 % | 56,0 | −26 ms | 77 ms | 636 ms |
| XS, 100 ms, 2 | 80 % | 21,0 % | 22,4 | 58 ms | 315 ms | 507 ms |
| XS, 60 ms, 3 | 80 % | 20,4 % | 23,3 | 40 ms | 263 ms | 553 ms |

Chọn **S, 100 ms, làm mượt 2, ngưỡng 95 %**:
* Cùng mức báo nhầm với bản cũ (19,8 /giờ), bỏ sót ít hơn (9,0 % so với 10,8 %), và **nhanh hơn 142 ms ở p50, 103 ms ở p90**.
* Trễ âm (dòng làm mượt 1) nghĩa là model bắn trước khi nói xong: nhanh nhưng báo nhầm gấp 3 lần, không dùng.
* XS nhanh hơn ở p50 nhưng bỏ sót gấp đôi, nên không đổi model.
* Không xuống dưới 80 ms (4 hop): một lần inference 65 ms cộng MFCC phải xong trước lần kế tiếp.
* Giá phải trả: CPU khi có người nói tăng từ ~35 % lên ~68 %. Luồng voice ưu tiên thấp nhất, nên vòng motor, safety và link vẫn chạy trước (đo ở §5.5).
* Trên rig, độ trễ tổng tới khi Linux có lệnh còn cộng thêm thời gian người nói câu lệnh và Vosk (CH-204); phần MCU là phần nhỏ nhất.

Chạy lại: `python eval_stream.py --arch s --stride 5 --smooth 2 --tag _st5_sm2` (kết quả trong `artifacts/stream_*_st*_sm*.json`).

## 5. Benchmark trên Pico 2 thật

Lệnh trong shell (cổng console USB): `kws bench`, `kws stream`, `kws stats`, `kws profile`, `kws cpu`, `kws vad on|off`.

### 5.1 Đúng port
* **S v2:** `kws bench` cho output int8 **trùng từng bit** với TFLite trên PC ở cả 14/14 clip (6 wake, 7 không phải wake, 1 tiếng ồn), phân loại 14/14 đúng.
* **XS:** 12/14 clip trùng, lệch tối đa 3 LSB (FFT của CMSIS-DSP lệch ~1e-6 so với FFT trên PC).
* **`kws stream`:** 14 clip chạy liên tiếp qua pipeline thật cho kết quả **6/6 wake được bắt, 0 báo nhầm**.

### 5.2 Thời gian — lịch sử tối ưu (mỗi bước là số đo, không ước lượng)
| Bước | DS-CNN-S | DS-CNN-XS | MFCC / frame |
|---|---|---|---|
| Zephyr mặc định (`cortex-m33+nodsp`, `-Os`) | 89,8 ms | 28,0 ms | 0,93 ms |
| + bật DSP extension + `-O2` | 62,2 ms | 21,8 ms | ~1,0 ms¹ |
| + trọng số copy vào SRAM | 56,4 ms | 20,2 ms | — |
| + bảng FFT CMSIS copy vào SRAM | — | — | **0,44 ms** (min) |
| **Build cuối, không khoá ngắt², SRAM** | **65,3 ms** | **24,1 ms** | 0,44 / 0,55 ms (min/avg) |

¹ đo khi ngắt đang chạy.
² Các dòng trên được đo với ngắt bị khoá. Bản cuối **bỏ khoá ngắt** vì khoá ngắt 60 ms sẽ chặn vòng điều khiển và watchdog trên rig thật. Số cuối vì thế đã gồm thời gian bị vòng điều khiển và các luồng khác chen vào, và dao động khoảng ±10 % giữa các lần build (xem 5.3).

### 5.3 Những gì phát hiện khi số đo "vô lý"
1. **Zephyr v4.2 tắt DSP của RP2350.** Nó build với `-mcpu=cortex-m33+nodsp`, nên CMSIS-NN rơi về code C thường. RP2350 có DSP extension. Sửa: `CONFIG_EAA_RP2350_DSP` select `ARMV8_M_DSP`. STM32U585 thì SoC đã tự bật.
2. **Dữ liệu hằng nằm trong flash là nút cổ chai, không phải CPU.**
   * Thanh ghi QMI cho thấy flash đọc kiểu **dual-SPI lệnh 0xBB, SCK 50 MHz**, cache XIP 16 KB, mỗi lần miss tốn ~150 cycle CPU.
   * Bằng chứng 1: FFT 512 điểm của CMSIS mất 84.600 cycle khi bảng twiddle ở flash.
   * Bằng chứng 2: cả frame MFCC từ 166k xuống 75k cycle sau khi copy bảng vào SRAM.
   * Bằng chứng 3: trọng số chuyển sang SRAM chỉ lợi 9 %, khớp với phần thời gian đọc 41 KB qua QSPI.
3. **Chuyển code (không phải dữ liệu) sang SRAM thì không lợi.**
   * Đã thử `CODE_DATA_RELOCATION` cho CMSIS-NN, CMSIS-DSP và front-end: tốn thêm 75 KB RAM mà không nhanh hơn.
   * Microbench `kws cpu`: cùng một vòng nhân-cộng float tốn ~8,6 cycle/MAC dù code ở SRAM hay flash, kể cả khi tắt ngắt. Trên M33, code ở SRAM phải chia bus với dữ liệu.
   * Đã tắt lại (`EAA_KWS_CODE_IN_RAM=n`) nhưng giữ option để ghi lại kết quả.
4. **DS-CNN-S 65 ms có hợp lý không?** Có. MLPerf Tiny KWS dùng đúng cỡ DS-CNN này (~2,7 M MAC). Các kết quả công bố cho Cortex-M33/M4 chạy TFLM + CMSIS-NN nằm trong khoảng vài chục ms. Kỳ vọng ban đầu "~1 cycle/MAC" là sai: lớp conv đầu chỉ có 1 kênh vào, rất kém hiệu quả với im2col.
5. **Lỗi thống kê:** tổng cycle 32-bit tràn sau ~430 lần chạy model và từng in "inference avg 5,6 ms". Đã đổi sang 64-bit.
6. **Benchmark không được khoá ngắt.** Bản đầu khoá ngắt trong lúc đo, trên rig thật sẽ treo vòng motor 60 ms. Đã bỏ.

### 5.4 Tải CPU và bộ nhớ (build cuối)
* **Im lặng:** MFCC 0,55 ms mỗi 20 ms = **2,7 %**.
* **Có người nói:** đo trên Pico khi VAD tắt và hệ thống đang chạy HIL: inference avg 64,0 ms, MFCC avg 0,76 ms → (64,0 + 5 × 0,76) ms mỗi 100 ms = **~68 %** (bản cũ 200 ms: ~35 %). Luồng voice ưu tiên 8, thấp hơn control (coop), safety (1), bridge (3), sensors (4).
* **RAM:** 236 KB / 520 KB (gồm 48 KB bản copy model, 23 KB arena, 32 KB buffer cho bench).
* **Flash:** 819 KB / 4 MB (gồm 448 KB clip bench). Tắt `CONFIG_EAA_KWS_BENCH` cho bản rig nếu cần.

### 5.5 Thông số embedded không bị phá (HIL 30/30, `hil_test.py`, model chạy liên tục mỗi 100 ms, VAD tắt, vòng điều khiển 100 Hz)
| Thông số | Yêu cầu | Đo |
|---|---|---|
| Jitter vòng điều khiển 100 Hz (NFR-01) | ≤ 1000 µs | **207 µs** max (exec 255 µs) qua 60 s, 9.543 vòng |
| Heartbeat watchdog | 300 ms | phát hiện sau 328 ms (gồm trễ USB và chu kỳ 10 ms của safety thread) |
| Telemetry | 20 Hz | 19,80 Hz, 0 frame lỗi |
| Retransmit event | ~100 ms | 11 bản trong 1 s, khoảng cách trung vị 117 ms |
| Lệnh RUN → ACK | — | 62 ms |
| Wake → VOICE_EVENT (lệnh `eaa wake`) | — | 62 ms |
| Lúc quá tải (S chạy mỗi 60 ms = 111 % nhu cầu) | — | jitter vẫn 330 µs (vòng điều khiển là coop, ưu tiên cao nhất) |

## 6. Vì sao không suy ra số của UNO Q (STM32U585)
Lõi giống nhau (Cortex-M33), nhưng hai chỗ quyết định tốc độ ở trên đều là chuyện riêng của Pico:
* (a) Zephyr tắt DSP trên RP2350.
* (b) Pico đọc flash qua QSPI có cache XIP. STM32U585 có flash trong chip với ART cache và 786 KB SRAM.

Vì vậy model và code giữ nguyên, nhưng **số đo phải làm lại trên UNO Q**: `kws bench` đã có sẵn và không cần micro.

## 7. Việc còn lại (theo thứ tự)
1. **Thu giọng thật.** Ít nhất 20 người trong team/xưởng, mỗi người khoảng 30 lần "Hey Edgie" và khoảng 30 câu dễ nhầm/lệnh, thu bằng chính INMP441, có ồn xưởng. Fine-tune từ model hiện tại, đo lại §4 với **người thật**: bỏ sót và báo nhầm/giờ.
2. **Gắn INMP441** (GP12 BCK, GP13 WS, GP14 SD), kiểm tra mức thu (`kws stats`: dBFS, noise floor), chỉnh `EAA_MIC_GAIN_SHIFT` và `EAA_KWS_VAD_DB`.
3. **Tầng xác minh trên MPU:** sau WAKE, nếu Vosk không ra lệnh hợp lệ trong 5 s thì bỏ qua và ghi log "false wake" để đo báo nhầm thực tế.
4. (Tuỳ chọn) Cấu hình XIP quad-SPI (0xEB) cho RP2350 để giảm thời gian đọc flash.
5. (Tuỳ chọn, cần team quyết) Thêm class "stop" từ Speech Commands.

## 8. File
| File | Vai trò |
|---|---|
| `kws/fe.py`, `make_dataset.py`, `train.py`, `eval_stream.py` | Toolkit train/đánh giá (README.md) |
| `pico2/firmware/src/voice/kws_frontend.c/.h` | MFCC dùng chung PC/MCU |
| `.../kws_pipeline.c` | VAD, stride, làm mượt, ngưỡng, refractory; backend `kws.h` cho `voice.c` |
| `.../kws_engine.cpp/.h` | TFLite Micro + CMSIS-NN, mutex giữa luồng voice và bench |
| `.../kws_bench.c` | Lệnh shell `kws ...` |
| `.../model/kws_s.cc`, `kws_xs.cc`, `kws_bench_clips.cc` | Model int8 + 14 clip test (sinh bởi `train.py`) |
| `pi/pico_shell.py` | Chạy lệnh shell / nạp UF2 từ PC |
| Kconfig `EAA_KWS_*`, `EAA_RP2350_DSP` | Mọi tham số ở trên |
