# Báo cáo: Firmware MCU trên Raspberry Pi Pico 2 (thay STM32U585 của UNO Q)

> Nhánh `embedded` · Jira: CH-235 (đề xuất), CH-236 (tổng), CH-237→CH-244 (M0–M7), CH-103 (wake word), CH-239 (bridge)
> Ngày: 2026-11-25 · Trạng thái: **firmware build xong và đã test logic trên máy tính; CHƯA chạy trên phần cứng thật** (Pico 2 chưa về).

---

## 1. Tóm tắt — đã làm gì

| Hạng mục | Trạng thái | Bằng chứng |
|---|---|---|
| Toolchain Zephyr v4.2.0 + SDK 0.17.2 trên Windows | ✅ cài xong | `embedded/pico2/tools/setup_zephyr.sh`, `firmware/build.ps1` |
| Firmware Zephyr cho `rpi_pico2/rp2350a/m33` | ✅ build, **0 warning** | Flash 86 KB / 4 MB, RAM 95 KB / 520 KB |
| File nạp sẵn `.uf2` (full + bench) | ✅ | `embedded/pico2/release/` |
| Protocol B-03 v1.1 (CRC16, COBS, 13 loại message) | ✅ test | 29/29 cross-check **trùng từng byte** với `codec.py` + `transport.py` của nhánh `orchestrator` |
| Safety FSM của MCU | ✅ test | 13 kịch bản fault-injection chạy trên PC (`safety_host_test.c`) |
| Điều khiển motor 200 Hz (open-loop, sẵn PI cho encoder) | ✅ code, ⏳ chưa đo jitter thật | thread tự đo jitter, lệnh `eaa status` |
| Sensor INA226, MLX90614, E18-D80NK, E-stop | ✅ code, ⏳ chưa có hàng | |
| Mic I2S qua PIO + wake word + kênh audio cho STT | ✅ code (KWS là **stub**), ⏳ chưa có model | Edge Impulse cắm vào là chạy (mục 7) |
| Công cụ phía Pi: `link_test.py`, `audio_rx.py`, `voice_to_belt.py` | ✅ test | `link_test.py` chạy với `MockMcu` thật của orchestrator: HELLO → RUN → telemetry → quá dòng → EVENT + ACK, 0 frame lỗi |

**Chưa làm / chưa kiểm được:** chạy trên Pico 2 thật, đo jitter thật, train model wake word, đo encoder (CH-107 còn bị chặn), nối `transport_profile: pipe` trong orchestrator (việc của nhánh orchestrator, CH-200).

---

## 2. Nguyên tắc: bám sát UNO Q nhất có thể

Mục tiêu không phải "chạy được trên Pico" mà là **mọi thứ làm trên Pico phải chuyển sang STM32U585 của UNO Q mà gần như không đổi**.

| Khía cạnh | UNO Q (đích) | Pico 2 + Pi 3B (tạm) | Lựa chọn và lý do |
|---|---|---|---|
| CPU MCU | 1× Cortex-M33 160 MHz | 2× Cortex-M33 150 MHz | **Chỉ dùng 1 core.** Không dùng core 1 dù Pico có, để mọi đo đạc jitter/CPU có ý nghĩa cho STM32. |
| RTOS | Zephyr | Zephyr v4.2.0 | Cùng RTOS, cùng API (GPIO, PWM, I2C, UART, watchdog). |
| RAM | 786 KB | 520 KB | Firmware dùng 95 KB → vừa cả hai, còn dư cho model wake word. |
| Code phụ thuộc board | — | **chỉ 2 file**: `boards/rpi_pico2_rp2350a_m33.overlay` và `src/voice/i2s_in_rp2.c` | Mọi chân lấy từ devicetree (`zephyr,user`, `chosen eaa,*`). Port = viết overlay mới + driver mic SAI. |
| Link MPU↔MCU | UART nội bộ (Arduino Bridge / `/dev/ttyHS1`) | UART0 GP0/GP1 ↔ Pi GPIO14/15, 115200 8N1 | Dùng đúng **Profile B (raw UART + COBS)** đã có trong `orchestrator/bridge/transport.py` và `uart_device` trong config. Sang UNO Q chỉ đổi tên thiết bị. |
| Protocol | B-03 v1.1 | B-03 v1.1 | Firmware được kiểm byte-by-byte với code Python của team. |
| Mic + wake word | Mic nối **MCU**, KWS chạy trên MCU (quyết định team, CH-102) | Mic I2S nối Pico, KWS chạy trên Pico, **cùng core với vòng motor** | Đúng kiến trúc đã chọn, nên phép đo "jitter khi có KWS" (NFR-01) có giá trị thật. |
| Kênh audio lệnh → STT | UART thứ 2 ≥ 921600 hoặc SPI (CH-103) | USB CDC ACM thứ 2 (`/dev/ttyACM1`) | Pi 3B chỉ có 1 UART tốt trên header. CDC ACM trong Zephyr **là một thiết bị UART** → code `audio_out.c` dùng UART API, sang UNO Q chỉ đổi `chosen eaa,audio-uart`. Phía Pi cũng chỉ là mở một cổng serial. |
| An toàn | MCU tự quyết, E-stop phần cứng | Y hệt | "MPU đề xuất, MCU quyết định". |

**Chỗ lệch so với CH-235 (ghi rõ để báo cáo):** CH-235 nói "core 1 lo link". Tao **không làm vậy** vì (1) Zephyr trên RP2350 chạy 1 core, (2) STM32U585 chỉ có 1 core, làm 2 core trên Pico thì kết quả đo không chuyển sang UNO Q được. Thay vào đó cách ly bằng độ ưu tiên thread.

---

## 3. Kiến trúc firmware

```
                    ┌──────────────── Pico 2 (Zephyr, 1 core) ─────────────────┐
 Pi 3B  UART0 ─────►│ bridge  (prio P3)  COBS/B-03, HELLO, CMD_ACK, TELEM 20 Hz │
 /dev/serial0 ◄─────│   │ lệnh                ▲ trạng thái/flags                 │
                    │   ▼                     │                                    │
                    │ safety  (prio P1, 100 Hz)  FSM + invariant + watchdog HW  ──┼─► event
                    │   │ run / setpoint / hard_stop                              │
                    │   ▼                                                         │
                    │ control (COOP 1, 200 Hz) ramp, open-loop/PI ──► PWM + EN ───┼─► BTS7960 ─► motor
                    │   ▲ E-stop pin đọc trực tiếp mỗi 5 ms                       │
                    │ sensors (P4, 50 Hz) INA226, MLX90614 (I2C), E18, E-stop     │
                    │ voice   (P8) mic I2S(PIO)→KWS 250 ms→ STOP/WAKE; audio ─────┼─► USB CDC1 ─► STT
 USB CDC0 ◄────────►│ shell/log  `eaa status|run|stop|clear|hb|wake|vstop`        │
                    └──────────────────────────────────────────────────────────────┘
```

Quy tắc sở hữu (để không ai "đi tắt" qua an toàn):
- **Chỉ `safety.c`** quyết định motor có được chạy không.
- **Chỉ `control.c`** ghi PWM và chân EN.
- **Chỉ `bridge.c`** gửi lên UART B-03 → frame không bao giờ chen lẫn nhau.

| Thread | Chu kỳ | Ưu tiên | Ghi chú |
|---|---|---|---|
| control | 5 ms (k_timer) | cooperative 1 (cao nhất) | Không bị thread nào chen ngang; thân vòng lặp ngắn (chỉ tính toán + ghi PWM). |
| safety | 10 ms | preempt 1 | Kiểm invariant, nuôi watchdog phần cứng 500 ms. |
| bridge | theo sự kiện + 50 ms | preempt 3 | |
| sensors | 20 ms (MLX 100 ms) | preempt 4 | I2C chậm, không bao giờ chạy trong vòng motor. |
| voice | 16 ms/khối, KWS mỗi 250 ms | preempt 8 | Thấp nhất trong các thread chức năng. |
| ISR mic (PIO0 IRQ) | 32 000 lần/s, rất ngắn | IRQ prio 1 | Đây là nguồn jitter chính phải đo ở M5/M6. DMA là tối ưu tiếp theo. |

---

## 4. Protocol B-03 đã implement

Đúng theo `orchestrator:src/control_pipeline/codec.py` (nguồn sự thật):
- Frame `AA | type | seq | ack | tick_ms | flags | len | payload ≤ 40 | CRC-16/CCITT-FALSE`, little-endian, bọc COBS + `0x00`.
- MPU→MCU: HELLO, HEARTBEAT, EVENT_ACK, RUN, SET_SPEED, STOP, HALT, CLEAR_FAULT.
- MCU→MPU: HELLO_ACK, TELEMETRY (20 Hz), MCU_EVENT, CMD_ACK, VOICE_EVENT.
- `flags` mọi frame: ESTOP_CLOSED, PE_CLEAR, HB_VALID, FAULT_LATCHED, MOTOR_ENABLED, SENSORS_OK, VOICE_ACTIVE.

Hành vi MCU:
| Tình huống | Firmware làm |
|---|---|
| Lệnh chuyển động trước khi có HELLO | REJECTED `NOT_SYNCED` |
| `cmd_id` trùng (8 lệnh gần nhất) | `DUPLICATE`, giữ nguyên lý do cũ, **không** thực hiện lại |
| `ttl_ms = 0` | REJECTED `EXPIRED` |
| setpoint > 500 / < 50 mm/s | `LIMIT_EXCEEDED` / `BAD_PARAM` |
| STOP | luôn chấp nhận, dừng có ramp (controlled stop) |
| HALT | luôn chấp nhận, cắt ngay, chốt lỗi `MPU_HALT_REQUEST` |
| Event | gửi lại mỗi 100 ms tới khi có EVENT_ACK, tối đa 20 lần; mỗi lần gửi lại có `seq` frame mới, **`event_seq` giữ nguyên** |

**Những điểm spec chưa rõ — cần team chốt (tao đã chọn tạm):**
1. **Orchestrator chưa lọc event trùng:** `worker.py` gửi ACK và gọi listener cho *mỗi* lần nhận, kể cả bản gửi lại. Cần lọc theo `event_seq` (link_test.py đã làm mẫu).
2. Retransmit 100 ms / 20 lần chưa có trong B-03 → đề xuất đưa vào v1.2.
3. Ý nghĩa TTL: MCU và MPU không đồng bộ đồng hồ nên không tính được "đã trễ bao nhiêu". Tạm: chỉ coi `ttl=0` là hết hạn.
4. Bit `sensor_status` (B-03 §4.3) chưa có trong code orchestrator → tạm: bit0 lỗi INA226, bit1 lỗi MLX, bit2 chưa có encoder, bit3 beam bị chắn, bit4 lỗi mic.
5. Jam: `mock_mcu.py` chỉ báo event mà **không dừng**. Firmware **dừng (SAFE_STOP)** khi beam bị chắn liên tục > 3 s lúc đang chạy — an toàn hơn; vật đi qua ngắn hơn 3 s thì bỏ qua.
6. `transport_profile: "pipe"` trong orchestrator chưa nối vào `pipeline.py` (chỉ chạy mock). Cách nối: mục 9.3.

---

## 5. Safety state machine

Trạng thái: `BOOT → STOPPED ⇄ RUNNING`, bất kỳ → `SAFE_STOP` (chốt). Khởi động mà E-stop đang mở → vào thẳng SAFE_STOP.

| Invariant | Ngưỡng (Kconfig) | Event / Reason | Hành động |
|---|---|---|---|
| E-stop mở | tức thì (đọc mỗi 5 ms trong vòng motor) | HARDWARE_ESTOP / ESTOP_ACTIVE | cắt PWM + EN ngay, chốt lỗi |
| E-stop đóng lại | — | ESTOP_RELEASED | vẫn chốt, cần CLEAR_FAULT |
| Mất heartbeat khi đang chạy | 300 ms | HEARTBEAT_TIMEOUT | cắt, chốt |
| Mất heartbeat khi đang dừng | 300 ms | — | chỉ xoá HB_VALID, từ chối RUN |
| Quá dòng | > 2500 mA kéo dài ≥ 60 ms | MOTOR_OVERLOAD / MOTOR_OVERCURRENT | cắt, chốt (bỏ qua xung dòng khởi động) |
| Quá nhiệt | > 80.0 °C | OVERTEMP / MOTOR_OVERTEMP | cắt, chốt |
| Kẹt băng | beam chắn ≥ 3 s khi chạy | JAM_DETECTED / PHOTOELECTRIC_BLOCKED | cắt, chốt |
| Mất sensor | 3 lần I2C lỗi liên tiếp | SENSOR_LOSS | cắt nếu đang chạy, từ chối RUN |
| Firmware treo | 500 ms | — | watchdog phần cứng reset chip; điện trở kéo xuống ở EN → driver tắt |

CLEAR_FAULT chỉ thành công khi **nguyên nhân đã hết** (E-stop đóng, có heartbeat, dòng/nhiệt bình thường, sensor OK); nếu không thì REJECTED kèm lý do còn tồn tại. Tất cả đã chạy thử trong `tests/host/safety_host_test.c`.

---

## 6. Điều khiển motor

- BTS7960: RPWM = PWM 20 kHz (ngoài vùng nghe), LPWM giữ 0 (băng chạy 1 chiều), R_EN + L_EN chung 1 chân.
- Ramp setpoint 250 mm/s² (Kconfig) để không giật băng và giảm xung dòng.
- **Open-loop (mặc định, vì CH-107 chưa có encoder):** `pwm‰ = 100 + 1.8 × mm/s`. Hai hệ số này **phải hiệu chuẩn**: chạy vài mức PWM, đo tốc độ băng thật bằng thước + đồng hồ, sửa `CONFIG_EAA_OPENLOOP_PWM_OFFSET_PM` và `..._GAIN_X100`. `speed_mms` trong telemetry lúc này là **ước lượng** — ghi rõ trong báo cáo.
- **Closed-loop (khi có encoder):** bật `CONFIG_EAA_ENCODER=y`, nhập số xung/vòng (×4) và chu vi con lăn → feed-forward + PI. Encoder giải mã bằng ngắt GPIO (code portable); trên STM32 có thể thay bằng timer encoder mode.
- Thread tự đo **jitter chu kỳ** và **thời gian thực thi** → `eaa status`; đây là số liệu cho NFR-01 (≤ 1 ms).

---

## 7. Wake word và giọng nói

### 7.1 Luồng xử lý
```
INMP441 ──I2S(PIO, 1.024 MHz BCK)──► ISR ──► ring 1 s ──► voice thread
   ├─► KWS mỗi 250 ms (cửa sổ 1 s, 16 kHz)
   │     "stop" ≥ 80% → MCU tự dừng băng (không cần wake word, không cần Pi) → VOICE_EVENT STOP
   │     wake ≥ 80%   → cờ VOICE_ACTIVE + LED GP15 + VOICE_EVENT WAKE → mở cửa sổ 5 s
   └─► trong cửa sổ: gửi PCM 16-bit qua USB CDC1 ─► Pi: audio_rx → Vosk STT → SLM
Nút push-to-talk GP16 = wake (để test khi chưa có model)
```

### 7.2 Mic I2S bằng PIO (chỗ duy nhất riêng của RP2350)
Zephyr chưa có driver I2S cho RP2350 nên tao viết chương trình PIO 8 lệnh (master, 64 BCK/frame, lấy mẫu sườn lên). Khi FIFO đầy, PIO dừng cả BCK nên **không bao giờ lệch kênh trái/phải** — chỉ mất mẫu (đếm `overruns`). Mẫu 24-bit → 16-bit bằng dịch phải `CONFIG_EAA_MIC_GAIN_SHIFT` (mặc định 7; nhỏ hơn = to hơn). Sang STM32: thay file này bằng driver SAI/I2S của Zephyr qua cùng `audio_in.h`.

### 7.3 Model wake word — trạng thái thật
Hiện firmware dùng **`kws_stub.c`: không nhận diện từ khoá nào**. Chạy được: mic, kênh audio, PTT, cờ, event, đường "stop" (giả lập bằng `eaa vstop`). Để có wake word thật:
1. **Dữ liệu:** "stop" có sẵn trong **Google Speech Commands** (team đã EDA ở nhánh `stt-tts`, `eda/eda_google_speech_commands.ipynb`). Cụm wake word nên **3 âm tiết** (CH-102: ít báo nhầm hơn); thu ~50–100 mẫu/người, nhiều người, kèm tiếng ồn băng chuyền thật (lớp `noise`) và câu nói khác (lớp `unknown`).
2. **Edge Impulse Studio:** 16 kHz, cửa sổ 1000 ms, MFCC, model nhỏ (1D-conv). Nhãn: `wake`, `stop`, `noise`, `unknown` (tên đổi được trong Kconfig).
3. **Export "C++ library"** → giải nén vào `embedded/pico2/firmware/ei-model/` (có `edge-impulse-sdk/`, `model-parameters/`, `tflite-model/`).
4. Build với `-DCONFIG_EAA_KWS_EDGE_IMPULSE=y`. Chế độ continuous 4 slice/cửa sổ → 1 quyết định/250 ms.
5. **Đo bắt buộc (CH-103/CH-243):** RAM/flash của model, thời gian suy luận/slice (`kws_result.time_us`), **jitter vòng motor khi KWS chạy** (`eaa jreset` rồi `eaa status`), số lần báo nhầm/giờ ở 60–90 dBA.

Phần tích hợp Edge Impulse (`kws_ei.cpp` + CMake) **chưa build thử được** vì chưa có model export — cần build lại khi có.

---

## 8. Nối với SLM (nhánh `slm`)

Model ABn (Qwen2.5-0.5B fine-tune, gọi tool kiểu `<tool_call>`, ~9–10 s/lệnh trên Pi 3B 1.4 GHz). `embedded/pi/voice_to_belt.py` nối toàn chuỗi: wake → audio → **Vosk** (giống `stt-pipeline/stt_adapter.py`) → `llama-server` với **đúng danh sách tool ABn được train** → lệnh B-03.

| Tool của SLM | Lệnh B-03 tới MCU | Ghi chú |
|---|---|---|
| `request_line_stop` | STOP | |
| `request_line_restart(target_speed_rpm)` | RUN (kind 1 = resume) | đổi rpm → mm/s |
| `request_set_speed(target_speed_rpm)` | SET_SPEED | đổi rpm → mm/s |
| `acknowledge_alarm` | CLEAR_FAULT | MCU chỉ cho clear khi lỗi đã hết |
| `get_live_telemetry` | — (đọc TELEMETRY mới nhất) | |
| `query_blackbox_logs`, `get_production_stats`, `get_inspection_summary`, `get_last_stop_reason`, `trigger_audio_alarm` | — | việc của orchestrator / database / HMI |
| `set_led_indicator` | **chưa có trong B-03** | đề xuất thêm message 0x15 SET_INDICATOR hoặc để HMI làm |

**Lệch đơn vị cần chốt:** SLM dùng **RPM** (`target_speed_rpm`), B-03 dùng **mm/s**. `mm/s = rpm × π × D_con_lăn / 60` → cần đo đường kính con lăn (`--roller-mm`). Trạng thái máy gửi cho SLM được map: STOPPED→IDLE, RUNNING→RUNNING, SAFE_STOP+jam→JAMMED, SAFE_STOP+quá nhiệt→OVERTEMP.

`voice_to_belt.py` là **demo, đi tắt qua orchestrator**. Hệ thống thật phải đi qua FSM + Safety Arbiter của orchestrator. Dù vậy MCU vẫn tự kiểm mọi lệnh nên đường demo không vượt được luật an toàn của MCU.

---

## 9. Phần cứng và đấu dây

### 9.1 Danh sách linh kiện (đối chiếu CH-83)
| Linh kiện | Ghi chú chọn |
|---|---|
| Raspberry Pi Pico 2 (RP2350A) | |
| Raspberry Pi 3B | đã có |
| BTS7960 IBT-2 | đang giao |
| Mic I2S **INMP441** (hoặc SPH0645) | 3.3 V, rẻ, phổ biến |
| **INA226** module, shunt 10 mΩ ≥ 2 W | bus tới 36 V → an toàn với 24 V (INA219 26 V là quá sát) |
| **MLX90614** bản 3.3 V (ESF/BAA-3V) | |
| E18-D80NK | nuôi 5 V |
| Nút E-stop nấm, **2 tiếp điểm NC** | 1 NC cắt nguồn motor, 1 NC báo về Pico |
| Relay/contactor 24 V (khuyến nghị) | để E-stop cắt nguồn motor qua contactor thay vì dòng motor đi thẳng qua nút |
| Nguồn 24 V, buck 24→5 V ≥ 3 A | |
| Điện trở: 10 kΩ ×2, 4.7 kΩ ×2, 1 kΩ ×1; diode 1N4148 ×1; LED ×1; nút nhấn ×1 | |

### 9.2 Bảng chân Pico 2
| Pico 2 | Chân vật lý | Nối tới | Lưu ý |
|---|---|---|---|
| GP0 (UART0 TX) | pin 1 | Pi **GPIO15 RXD** (header pin 10) | chéo TX→RX |
| GP1 (UART0 RX) | pin 2 | Pi **GPIO14 TXD** (header pin 8) | |
| GND | pin 3 | Pi GND (header pin 6) | **bắt buộc chung mass** |
| GP2 (PWM1A) | pin 4 | BTS7960 **RPWM** | |
| GP3 (PWM1B) | pin 5 | BTS7960 **LPWM** | luôn 0 |
| GP4 | pin 6 | BTS7960 **R_EN + L_EN** (nối chung) | **10 kΩ kéo xuống GND** → tắt khi Pico reset |
| GP6 / GP7 | pin 9 / 10 | encoder A / B | chỉ khi có encoder |
| GP8 (I2C0 SDA) | pin 11 | SDA của INA226 và MLX90614 | 4.7 kΩ kéo lên **3V3** |
| GP9 (I2C0 SCL) | pin 12 | SCL của INA226 và MLX90614 | 4.7 kΩ kéo lên 3V3 |
| GP10 | pin 14 | E18-D80NK dây đen (OUT) qua diode | xem 9.4 |
| GP11 | pin 15 | tiếp điểm NC phụ của E-stop → GND | mở = E-stop |
| GP12 | pin 16 | INMP441 **SCK** | |
| GP13 | pin 17 | INMP441 **WS** | |
| GP14 | pin 19 | INMP441 **SD** | |
| GP15 | pin 20 | LED "đang nghe" (+ 1 kΩ) → GND | |
| GP16 | pin 21 | nút push-to-talk → GND | |
| 3V3 OUT | pin 36 | VDD INA226, MLX90614, INMP441, pull-up | |
| VBUS/USB | micro-USB | cổng USB của Pi 3B | cấp nguồn Pico + console + audio |

### 9.3 Sơ đồ nối dây

**(a) Pi 3B ↔ Pico 2 (đủ cho M2, test ngay khi Pico về)**
```
Pi 3B header                          Pico 2
pin 8  GPIO14 TXD ──────────────────► GP1  (UART0 RX)
pin 10 GPIO15 RXD ◄────────────────── GP0  (UART0 TX)
pin 6  GND        ─────────────────── GND
USB-A             ═══ cáp micro-USB ═ USB  (nguồn + /dev/ttyACM0 console + /dev/ttyACM1 audio)
Pico GP11 ── jumper ── GND            (giả lập E-stop "đóng" khi chưa có nút)
```
Cả hai đều 3.3 V → không cần chuyển mức. Không nối 5 V của Pi vào Pico khi đã cắm USB.

**(b) Nguồn và motor**
```
 24 V PSU (+) ──[E-stop NC #1]──[contactor]──┬──► BTS7960 B+
                                            └──► INA226 VIN+ ─[shunt 10 mΩ]─ VIN- ─► (đo dòng phía cao)
 24 V PSU (–) ──────────────────────────────────► BTS7960 B-  ──── GND chung ─── Pico GND ── Pi GND
 BTS7960 M+ / M- ─────────────────────────────► motor băng chuyền
 BTS7960 VCC (logic) ◄── 3V3 của Pico           (xem (c))
 24 V ──► buck 5 V 3 A ──► Pi 3B (5 V/2.5 A) ;  Pico lấy nguồn từ USB của Pi
```
- INA226 đặt **phía cao (high-side)** nối tiếp với B+ để đo đúng dòng motor; bus ≤ 36 V. Đặt **phía thấp** cũng được nhưng khi đó GND motor không còn trùng GND logic.
- Cuộn dây contactor/relay cần diode dập. Dây motor và dây tín hiệu đi tách nhau, xoắn đôi dây I2S/I2C nếu dài.

**(c) BTS7960 (IBT-2)**
```
IBT-2   RPWM ◄── GP2        R_EN ◄──┬── GP4
        LPWM ◄── GP3        L_EN ◄──┘   └── 10 kΩ ── GND
        R_IS, L_IS  (bỏ trống; dòng đo bằng INA226)
        VCC ◄── 3V3              GND ── GND chung
```
Cấp **VCC logic của IBT-2 bằng 3V3**: module có IC đệm 74HC244; nếu nuôi 5 V thì IC này cần mức cao ≥ 3.5 V, tín hiệu 3.3 V của Pico sẽ không chắc chắn. Nuôi 3V3 thì đệm ra 3.3 V, vẫn đủ ngưỡng của BTS7960.

**(d) I2C: INA226 + MLX90614**
```
3V3 ──┬── 4.7k ──┬── GP8 SDA ── SDA INA226 ── SDA MLX90614
      └── 4.7k ──┼── GP9 SCL ── SCL INA226 ── SCL MLX90614
                 │  INA226 A0=A1=GND → 0x40 ;  MLX90614 mặc định 0x5A
GND ─────────────┴── GND cả hai ;   VCC cả hai ← 3V3
```
MLX90614 giới hạn 100 kHz → overlay đã đặt `I2C_BITRATE_STANDARD`. Ống kính MLX hướng vào thân motor (đo nhiệt vỏ motor).

**(e) Cảm biến quang E18-D80NK (5 V, ngõ ra NPN)**
```
E18 nâu  (VCC) ◄── 5 V
E18 xanh (GND) ─── GND
E18 đen  (OUT) ──|◄── GP10        (diode 1N4148: vạch/cathode về phía E18)
                       GP10 dùng pull-up nội lên 3V3
```
Diode giúp E18 kéo GP10 xuống khi có vật, nhưng không bao giờ đẩy 5 V vào chân Pico (nhiều bản E18 có điện trở kéo lên 5 V bên trong). Có vật / beam bị chắn = mức thấp.

**(f) E-stop**
```
Nút E-stop (2 tiếp điểm NC):  NC#1 nối tiếp mạch nguồn motor / cuộn contactor (cắt phần cứng, Category 0)
                              NC#2: một đầu → GP11, đầu kia → GND
Bình thường NC#2 đóng → GP11 = 0 (OK).  Nhấn E-stop → mở → pull-up kéo GP11 = 1 → MCU báo HARDWARE_ESTOP.
```
Firmware **không** thay thế mạch cắt phần cứng; nó chỉ báo cáo và cắt PWM thêm một lớp.

**(g) Mic INMP441**
```
INMP441 VDD ◄── 3V3   GND ── GND   L/R ── GND (kênh trái)
        SCK ◄── GP12  WS ◄── GP13   SD ──► GP14
```

**(h) LED và nút test**
```
GP15 ── 1 kΩ ── LED ── GND        (sáng = đang nghe lệnh)
GP16 ── nút ── GND                (push-to-talk; pull-up nội)
LED on-board GP25: nháy chậm = OK, nháy nhanh = SAFE_STOP
```

### 9.4 Lưu ý điện
- **Chung mass** Pi – Pico – BTS7960 – nguồn 24 V (–).
- RP2350 có lỗi silicon **E9** (pull-down nội có thể bị giữ ~2 V): firmware chỉ dùng **pull-up nội**, chân EN dùng **pull-down ngoài** 10 kΩ.
- Pi 3B cần nguồn 5 V khoẻ (team đã gặp sụt áp ở `results_pi`) — nguồn yếu làm Pi reset → Pico mất heartbeat → băng tự dừng (đúng thiết kế, nhưng phiền khi demo).

---

## 10. Cài đặt, build, nạp

### 10.1 Trên máy Windows (đã cài sẵn trên máy này)
- Toolchain ở `%USERPROFILE%\zephyrproject` (Zephyr v4.2.0, SDK 0.17.2, venv có west/cmake/ninja/zig).
- Máy khác: chạy `embedded/pico2/tools/setup_zephyr.sh` trong Git Bash (đọc 3 dòng đầu).
- Build:
  ```powershell
  cd embedded\pico2\firmware
  .\build.ps1          # bản full -> release\eaa_mcu_pico2_full.uf2
  .\build.ps1 bench    # bản bench (chưa có INA226/MLX90614)
  .\build.ps1 test     # unit test trên PC
  ```

### 10.2 Nạp vào Pico 2
1. Giữ nút **BOOTSEL**, cắm USB → hiện ổ `RP2350`.
2. Kéo `release/eaa_mcu_pico2_bench.uf2` (lần đầu, chưa có sensor) hoặc `..._full.uf2` vào ổ đó. Pico tự khởi động lại.
3. LED GP25 nháy chậm. Nếu nháy nhanh: đang SAFE_STOP (thường là GP11 chưa nối GND).

### 10.3 Phía Pi 3B
```bash
sudo raspi-config          # Interface > Serial: login shell = No, hardware = Yes
echo -e "enable_uart=1\ndtoverlay=disable-bt" | sudo tee -a /boot/firmware/config.txt
sudo systemctl disable hciuart; sudo reboot
pip3 install pyserial vosk
ls /dev/serial0 /dev/ttyACM*     # serial0 = B-03, ttyACM0 = console, ttyACM1 = audio
```
Nối vào orchestrator thật (PR nhỏ bên nhánh orchestrator, CH-200):
```python
import serial_asyncio  # pip install pyserial-asyncio
reader, writer = await serial_asyncio.open_serial_connection(url="/dev/serial0", baudrate=115200)
pipeline = OrchestratorPipeline(settings, transport=PipeTransport(reader, writer, max_frame_bytes=64))
```

### 10.4 Console của Pico (`/dev/ttyACM0` hoặc COMx, 115200)
`eaa status` · `eaa run 150` · `eaa stop` · `eaa clear` · `eaa hb` · `eaa wake` · `eaa vstop` · `eaa jreset`
(`eaa run` chỉ để test trên bàn: cần `eaa hb` trong vòng 300 ms, nếu không MCU tự SAFE_STOP — chính là luật heartbeat.)

---

## 11. Quy trình test khi Pico về (theo milestone Jira)

| # | Test | Cách làm | Đạt khi |
|---|---|---|---|
| T1 (M0) | Khởi động | nạp bench uf2, mở console | log `EAA MCU fw 0x0100`, `eaa status` chạy |
| T2 (M2) | Link soak | `python3 link_test.py --soak 600` | telemetry ≈ 20 Hz, **0 frame lỗi**, 10 phút |
| T3 (M2) | MPU treo | trong `link_test.py`: `run 150` rồi `freeze 1000` | SAFE_STOP ≤ 300 ms + event HEARTBEAT_TIMEOUT |
| T4 (M2) | Rút dây TX của Pi | rút GP1 khi đang chạy | như T3; cắm lại → HELLO lại được |
| T5 (M3) | E-stop | gỡ jumper GP11 khi chạy | HARDWARE_ESTOP, PWM = 0 ngay; nối lại → ESTOP_RELEASED; `clear` → STOPPED |
| T6 (M3) | PWM không tải | `run 100`, đo GP2 bằng đồng hồ/scope | 20 kHz, duty ≈ 28%; EN = 1 |
| T7 (M3) | Sensor | bản full, `eaa status` | dòng/nhiệt hợp lý; rút sensor → SENSOR_LOSS |
| T8 (M4) | Fault injection | chắn beam > 3 s, nung MLX, quá tải motor, kill `link_test.py` | mỗi case → SAFE_STOP đúng event + reason |
| T9 (M5) | Jitter | `eaa jreset`, chạy 10 phút có link + sensor + mic, `eaa status` | jitter max ≤ 1000 µs (NFR-01) |
| T10 (M6) | Audio | `python3 audio_rx.py`, nhấn PTT, nói 5 s | ra file WAV 5 s nghe rõ |
| T11 (M6) | Voice stop không cần Pi | kill hết script trên Pi, `eaa vstop` (sau này: nói "stop") | băng dừng |
| T12 (M7) | Đầu-cuối | `voice_to_belt.py` + llama-server ABn | nói "stop the belt" → băng dừng; đo thời gian STT/SLM |

Ghi số liệu với nhãn **"Pico 2 + Pi 3B (proxy)"** như quy ước ở CH-234.

---

## 12. Đã kiểm tra gì (trên máy tính)

| Kiểm tra | Kết quả |
|---|---|
| Build firmware full và bench, Zephyr v4.2.0 | ✅ 0 warning; full: Flash 86.9 KB, RAM 95.1 KB |
| `b03_host_test` (CRC check value 0x29B1, vector vàng TELEMETRY, COBS 0–300 byte, lỗi CRC/magic/len) | ✅ pass |
| `cross_check.py`: C ↔ `codec.py`/`transport.py` của orchestrator, cả hai chiều | ✅ 29/29 trùng byte |
| `safety_host_test` (13 kịch bản) | ✅ pass |
| `b03.py --selftest` so với orchestrator | ✅ |
| `link_test.py` ↔ `MockMcu` thật của orchestrator qua TCP | ✅ HELLO, RUN, telemetry, MOTOR_OVERLOAD + ACK, 0 frame lỗi |
| Parser `audio_rx.py` (luồng tổng hợp có rác ở đầu) | ✅ |
| Chạy trên Pico 2 thật, PIO I2S, USB CDC, jitter, sensor thật | ❌ **chưa** — cần phần cứng |
| Build với model Edge Impulse | ❌ **chưa** — chưa có model |

---

## 13. Khi UNO Q về: checklist port

1. Viết overlay cho board UNO Q (MCU STM32U585): cùng tên `zephyr,user` + `chosen eaa,*`, map sang chân JDIGITAL/Qwiic.
2. `eaa,bridge-uart` → UART nội bộ tới QRB2210; phía Linux đổi `uart_device` (hoặc Profile A `arduino-router`).
3. `eaa,audio-uart` → UART thứ 2 ≥ 921600 hoặc SPI (kiểm có đi ra header không — CH-103 mục 5).
4. Thay `i2s_in_rp2.c` bằng driver SAI/I2S Zephyr theo `audio_in.h`.
5. Encoder: dùng timer encoder mode (Zephyr `qdec_stm32`).
6. Chạy lại toàn bộ mục 11 và so số với Pico 2.

---

## 14. Bản đồ file

```
embedded/
├── pico2/
│   ├── PICO2_PLAN.md          kế hoạch ban đầu (CH-236)
│   ├── REPORT.md              báo cáo này
│   ├── release/               eaa_mcu_pico2_full.uf2, eaa_mcu_pico2_bench.uf2
│   ├── tools/setup_zephyr.sh  cài toolchain 1 lần
│   └── firmware/
│       ├── CMakeLists.txt, prj.conf, Kconfig, build.ps1
│       ├── boards/rpi_pico2_rp2350a_m33.overlay|.conf   ← toàn bộ pin map
│       ├── src/main.c          khởi động + shell
│       ├── src/eaa.h           giao diện các module
│       ├── src/safety.c        FSM an toàn
│       ├── src/control.c       vòng 200 Hz
│       ├── src/motor.c, encoder.c, sensors.c, bridge.c
│       ├── src/proto/          b03.c, cobs.c, crc16.c (C thuần)
│       ├── src/voice/          voice.c, i2s_in_rp2.c, audio_out.c, kws_stub.c, kws_ei.cpp
│       └── tests/host/         b03_host_test.c, safety_host_test.c, cross_check.py, shim/
└── pi/                         b03.py, link_test.py, audio_rx.py, voice_to_belt.py
```
