# Báo cáo: Firmware MCU trên Raspberry Pi Pico 2 (thay STM32U585 của UNO Q)

> Nhánh `embedded` · Jira: CH-235 (đề xuất), CH-236 (tổng), CH-237→CH-244 (M0–M7), CH-103 (wake word), CH-239 (bridge)
> Cập nhật: 2026-11-26 · Trạng thái: **đã chạy trên Pico 2 thật (cắm USB vào PC), 30/30 test phần cứng đạt; đã build được cho UNO Q (STM32U585) nhưng chưa có board để chạy.** Chưa nối dây Pi 3B, motor, sensor, mic.

---

## 1. Tóm tắt — đã làm gì

| Hạng mục | Trạng thái | Bằng chứng |
|---|---|---|
| Toolchain Zephyr v4.2.0 + SDK 0.17.2 trên Windows | ✅ cài xong | `embedded/pico2/tools/setup_zephyr.sh`, `firmware/build.ps1` |
| Firmware Zephyr cho `rpi_pico2/rp2350a/m33` | ✅ build, **0 warning** | Flash 86 KB / 4 MB, RAM 95 KB / 520 KB |
| File nạp sẵn `.uf2` (full + bench) | ✅ | `embedded/pico2/release/` |
| Protocol B-03 v1.1 (CRC16, COBS, 13 loại message) | ✅ test | 29/29 cross-check **trùng từng byte** với `codec.py` + `transport.py` của nhánh `orchestrator` |
| Safety FSM của MCU | ✅ test | 13 kịch bản fault-injection chạy trên PC (`safety_host_test.c`) |
| Điều khiển motor **100 Hz** (open-loop; vì sao không cần 200 Hz: mục 6.1) | ✅ **đo trên Pico thật** | jitter max **207 µs** / 60 s khi wake word chạy liên tục (giới hạn NFR-01: 1000 µs = 10 % chu kỳ), thân vòng lặp max 255 µs |
| Cắt motor khi có sự cố (T1) | ✅ **đo trên Pico thật** | E-stop qua ngắt và mọi trip của safety cắt driver trực tiếp, không chờ vòng điều khiển (mục 6.2) |
| Link B-03 + safety FSM trên Pico thật | ✅ **30/30** | `embedded/pi/hil_test.py` (mục 12.1) |
| Watchdog phần cứng | ✅ **đo thật** | firmware cố ý treo → chip reset sau ~0.55 s (500 ms + 50 ms trễ lệnh) |
| Port sang UNO Q (STM32U585) | ✅ build, ⏳ chưa có board | cùng code + cùng thông số với Pico; Flash 811 KB / 2 MB, RAM 174 KB / 768 KB (gồm wake word và clip bench; mục 13) |
| Sensor INA226, MLX90614, E18-D80NK, E-stop | ✅ code, ⏳ chưa có hàng | |
| Mic I2S qua PIO + wake word + kênh audio cho STT | ✅ model "Hey Edgie" chạy trên Pico (chưa có mic: test bằng clip trong flash) | `kws/WAKEWORD_REPORT.md` |
| Công cụ phía Pi: `link_test.py`, `audio_rx.py`, `voice_to_belt.py` | ✅ test | `link_test.py` chạy với `MockMcu` thật của orchestrator: HELLO → RUN → telemetry → quá dòng → EVENT + ACK, 0 frame lỗi |

**Chưa làm / chưa kiểm được:** nối dây UART thật với Pi 3B (link đã test qua USB, cùng code), motor + BTS7960, sensor, mic (bỏ qua theo yêu cầu), giọng người thật cho wake word, encoder (không thuộc phase này, CH-107), chạy trên UNO Q thật, nối `transport_profile: pipe` trong orchestrator (CH-200).

---

## 2. Nguyên tắc: bám sát UNO Q nhất có thể

Mục tiêu không phải "chạy được trên Pico" mà là **mọi thứ làm trên Pico phải chuyển sang STM32U585 của UNO Q mà gần như không đổi**.

| Khía cạnh | UNO Q (đích) | Pico 2 + Pi 3B (tạm) | Lựa chọn và lý do |
|---|---|---|---|
| CPU MCU | 1× Cortex-M33 160 MHz | 2× Cortex-M33 150 MHz | **Chỉ dùng 1 core.** Không dùng core 1 dù Pico có, để mọi đo đạc jitter/CPU có ý nghĩa cho STM32. |
| RTOS | Zephyr | Zephyr v4.2.0 | Cùng RTOS, cùng API (GPIO, PWM, I2C, UART, watchdog). |
| RAM | 786 KB | 520 KB | Firmware + wake word dùng 236 KB (bench), vừa cả hai. |
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
                    │ control (COOP 1, 100 Hz) ramp, open-loop ──► PWM + EN ──────┼─► BTS7960 ─► motor
                    │   ▲ E-stop: ngắt GPIO cắt EN ngay + đọc mức mỗi 10 ms       │
                    │ sensors (P4, 50 Hz) INA226, MLX90614 (I2C), E18, E-stop     │
                    │ voice   (P8) mic I2S(PIO)→MFCC+VAD→KWS 100 ms→ WAKE   ; audio ─────┼─► USB CDC1 ─► STT
 USB CDC0 ◄────────►│ shell/log  `eaa status|run|stop|clear|hb|wake|vstop`        │
                    └──────────────────────────────────────────────────────────────┘
```

Quy tắc sở hữu (để không ai "đi tắt" qua an toàn):
- **Chỉ `safety.c`** quyết định motor có được chạy không.
- **Chỉ `control.c`** bật PWM và chân EN. Đường cắt `motor_kill()` (safety trip, ngắt E-stop) chỉ được **tắt**, và khi đã cắt thì `control.c` không bật lại được cho tới CLEAR_FAULT.
- **Chỉ `bridge.c`** gửi lên UART B-03 → frame không bao giờ chen lẫn nhau.

| Thread | Chu kỳ | Ưu tiên | Ghi chú |
|---|---|---|---|
| control | 10 ms (k_timer) | cooperative 1 (cao nhất) | Không bị thread nào chen ngang; thân vòng lặp ngắn (chỉ tính toán + ghi PWM). |
| safety | 10 ms | preempt 1 | Kiểm invariant, nuôi watchdog phần cứng 500 ms. |
| bridge | theo sự kiện + 50 ms | preempt 3 | |
| sensors | 20 ms (MLX 100 ms) | preempt 4 | I2C chậm, không bao giờ chạy trong vòng motor. |
| voice | 20 ms/hop, KWS mỗi 100 ms khi có tiếng nói | preempt 8 | Thấp nhất trong các thread chức năng: wake word cần **độ trễ** thấp nhất, nhưng không được chiếm CPU trước an toàn. |
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
| E-stop mở | ngắt GPIO (cạnh mở), cộng đọc mức mỗi 10 ms | HARDWARE_ESTOP / ESTOP_ACTIVE | cắt EN + PWM ngay trong ngắt, chốt lỗi |
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
- **Encoder không thuộc phase này** (CH-107). Code closed-loop cũ vẫn để tắt (`CONFIG_EAA_ENCODER=n`), không dùng để biện minh cho tần số vòng.
- Thread tự đo **jitter chu kỳ** và **thời gian thực thi** → `eaa status`; đây là số liệu cho NFR-01 (≤ 1 ms).

### 6.1 Tần số vòng điều khiển: vì sao 100 Hz, không phải 200 Hz

Câu hỏi đúng là "hệ thống cần gì", không phải "nhanh hơn thì tốt hơn". 200 Hz trước đây là con số chọn trước rồi mới đặt jitter 1 ms quanh nó; chưa có phép tính nào bắt buộc 200 Hz. Ở phase này (open-loop, chưa có encoder), vòng điều khiển chỉ làm 3 việc. Đây là yêu cầu của từng việc:

| Việc vòng phải làm | Yêu cầu thật | Ở 200 Hz | Ở 100 Hz | Kết luận |
|---|---|---|---|---|
| Ramp setpoint 250 mm/s² | bước nhỏ hơn nhiều so với tốc độ băng đáp ứng được | 1,25 mm/s mỗi tick (2,3 ‰ PWM) | 2,5 mm/s mỗi tick (4,5 ‰ PWM) | Motor giảm tốc 24 V + băng có hằng số thời gian cơ cỡ hàng chục ms trở lên (chưa đo, CH-83), nên cả hai đều mượt. 100 Hz đủ. |
| Giữ driver tắt khi SAFE_STOP / E-stop | phải cắt ngay, không phụ thuộc chu kỳ | trước đây chờ tới tick kế: ≤ 5,3 ms | trước đây ≤ 10,3 ms | **Đã tách khỏi vòng** (6.2). Tần số vòng không còn quyết định T1. |
| Đọc sensor | E18-D80NK (phát hiện kẹt sau 3 s), INA226 (lọc quá dòng 60 ms), MLX90614 (nhiệt, chậm) | — | — | Sensor đọc ở thread riêng 50 Hz, không nằm trong vòng. Không sensor nào cần 200 Hz. |

Các con số khác của belt:
* **Quãng chạy trong 1 chu kỳ ở tốc độ tối đa 500 mm/s:** 2,5 mm ở 200 Hz, 5 mm ở 100 Hz. Chỉ có ý nghĩa nếu việc dừng phải chờ tick, và giờ thì không còn phải chờ.
* **Quãng chạy trong T1 đo được (≤ 22 µs):** 0,01 mm ở 500 mm/s. Quãng trôi sau khi cắt (do quán tính băng) lớn hơn rất nhiều và cần đo trên rig.
* **Jitter:** giới hạn NFR-01 1 ms bằng **10 % chu kỳ ở 100 Hz** (mức thường dùng cho vòng lấy mẫu). Ở 200 Hz, cùng 1 ms lại là 20 %, tức quá lỏng. Đo được 207 µs, bằng 2 % chu kỳ.
* **CPU:** thân vòng max 255 µs → ≤ 2,6 % ở 100 Hz (≤ 5,1 % ở 200 Hz). Tiết kiệm khoảng 2,5 % CPU, nhưng **không phải** lý do chính: wake word nhanh lên là nhờ stride của nó (WAKEWORD_REPORT §4.4), không nhờ phần CPU này.

**Khi nào phải tính lại:** khi có encoder (không thuộc phase này) và đo được hằng số thời gian cơ τ của băng. Công thức: băng thông vòng tốc độ f_bw ≈ vài lần 1/(2πτ), lấy mẫu ≥ 20 × f_bw. Nếu τ < ~30 ms thì 100 Hz không còn đủ dư. `CONFIG_EAA_CONTROL_PERIOD_US=5000` vẫn build và pass test, nên đổi lại chỉ là một dòng cấu hình.

### 6.2 T1: từ lúc phát hiện sự cố tới lúc cắt driver

T1 là yêu cầu **đầu-cuối riêng**, đo riêng, không suy ra từ tần số vòng. Firmware giờ cắt driver trực tiếp, không chờ vòng điều khiển:
* **E-stop:** ngắt GPIO khi contact mở → `motor_kill()` kéo R_EN/L_EN xuống thấp ngay trong ngắt (không debounce: nhiễu chỉ gây dừng an toàn có chốt, không bao giờ làm lỡ một lần dừng thật).
* **Mọi trip của safety** (HALT từ MPU, mất heartbeat, quá dòng, quá nhiệt, kẹt, mất sensor): `trip()` gọi `motor_kill()` ngay khi phát hiện.
* Khi đã cắt, `motor_apply()` của vòng điều khiển không bật lại được cho tới CLEAR_FAULT. Kiểm-và-ghi có khoá nên ngắt chen vào giữa cũng không bật lại được.

Đo trên Pico 2 (`eaa t1test`: ép đầu vào pad E-stop thành "mở" bằng `INOVER` của RP2350, nên ngắt chạy đúng đường GPIO thật; 20 lần khi băng đang chạy 300 mm/s, wake word chạy liên tục):

| Đường cắt | T1 đo được (20 lần) |
|---|---|
| **Trực tiếp (bản mới)**: phát hiện → EN xuống thấp | **13–22 µs** |
| Chờ tick vòng 100 Hz (chỉ để so sánh) | 0,3–9,6 ms (phụ thuộc pha) |

Chưa gồm trong số đo: trễ từ chân pad vào ngắt (vài µs, cần logic analyzer) và thời gian BTS7960 tắt. Trên rig, E-stop còn có relay cứng cắt nguồn motor, độc lập với MCU.

### 6.3 Pico 2 và STM32U585: dùng chung thông số được không
Được, và đó là cách đang làm: mọi thông số thời gian nằm trong Kconfig, giống nhau cho cả hai board. Pico 2 là bản **chậm hơn** (đọc flash qua QSPI, Zephyr tắt DSP mặc định), nên nếu thông số đạt trên Pico thì có dư trên STM32U585 (flash trong chip có ART cache, 160 MHz). Không có hệ số "scale" nào để biến số đo trên Pico thành số của UNO Q. Bản UNO Q build cùng code, cùng Kconfig (vòng 100 Hz, stride wake 100 ms, ngưỡng 95 %, đường cắt trực tiếp). `eaa t1test` trên STM32U585 dùng thanh ghi `EXTI->SWIER1` để kích đúng đường ngắt E-stop (thay cho `INOVER` của RP2350). Khi có board thì chạy lại `hil_test.py`, `eaa t1test` và `kws bench`.

---

## 7. Wake word và giọng nói

### 7.1 Luồng xử lý
Chi tiết, số đo và benchmark: **[../kws/WAKEWORD_REPORT.md](../kws/WAKEWORD_REPORT.md)**.
```
INMP441 ──I2S(PIO, 1.024 MHz BCK)──► ISR ──► ring ──► voice thread, mỗi 20 ms:
   ├─► MFCC 49×10 + VAD năng lượng (0.5 ms, luôn chạy = 2.7 % CPU)
   ├─► chỉ khi có tiếng nói: DS-CNN-S int8 "Hey Edgie" mỗi 100 ms (65 ms/lần ≈ 68 % CPU), trễ wake p50 80 ms
   │     trung bình 2 điểm ≥ 95 % → cờ VOICE_ACTIVE + LED GP15 + VOICE_EVENT WAKE → cửa sổ 5 s
   └─► trong cửa sổ: gửi PCM 16-bit qua USB CDC1 ─► Pi: audio_rx → Vosk STT → SLM
Nút push-to-talk GP16 / `eaa wake` = wake (test). "stop" nói ra: đường code còn (`eaa vstop`),
model hiện chỉ có wake (silence / unknown / wake).
```

### 7.2 Mic I2S bằng PIO (chỗ duy nhất riêng của RP2350)
Zephyr chưa có driver I2S cho RP2350 nên tao viết chương trình PIO 8 lệnh (master, 64 BCK/frame, lấy mẫu sườn lên). Khi FIFO đầy, PIO dừng cả BCK nên **không bao giờ lệch kênh trái/phải** — chỉ mất mẫu (đếm `overruns`). Mẫu 24-bit → 16-bit bằng dịch phải `CONFIG_EAA_MIC_GAIN_SHIFT` (mặc định 7; nhỏ hơn = to hơn). Sang STM32: thay file này bằng driver SAI/I2S của Zephyr qua cùng `audio_in.h`.

### 7.3 Model wake word — trạng thái thật
Đã có model thật: **DS-CNN-S int8 "Hey Edgie"** (TFLite Micro + CMSIS-NN), train bằng giọng TTS (≈ 1.000 người nói tổng hợp) + Google Speech Commands. Trên Pico 2: output trùng từng bit với PC (14/14 clip), HIL 30/30 khi model chạy liên tục, jitter 231 µs. Giới hạn: **chưa có giọng người thật** → phải thu giọng team rồi fine-tune (WAKEWORD_REPORT.md §7). Đường Edge Impulse (`kws_ei.cpp`) vẫn giữ làm phương án thay thế, không còn là mặc định.

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
- Toolchain ở `%USERPROFILE%\zephyrproject` (Zephyr v4.2.0 + `hal_rpi_pico` + `hal_stm32`, SDK 0.17.2, venv có west/cmake/ninja/zig).
- Máy khác: chạy `embedded/pico2/tools/setup_zephyr.sh` trong Git Bash (đọc 3 dòng đầu; thêm `+hal_stm32` vào filter nếu cần bản UNO Q).
- Build (nếu PowerShell chặn script: `powershell -ExecutionPolicy Bypass -File .\build.ps1 <bản>`):
  ```powershell
  cd embedded\pico2\firmware
  .\build.ps1          # full   -> release\eaa_mcu_pico2_full.uf2   (có INA226 + MLX90614)
  .\build.ps1 bench    # bench  -> release\eaa_mcu_pico2_bench.uf2  (chưa có sensor)
  .\build.ps1 pctest   # pctest -> release\eaa_mcu_pico2_pctest.uf2 (test trên PC, KHÔNG dùng trên rig)
  .\build.ps1 unoq     # UNO Q  -> release\eaa_mcu_unoq.bin        (STM32U585, chưa chạy thật)
  .\build.ps1 test     # unit test trên PC
  ```

### 10.2 Nạp vào Pico 2
- Lần đầu: giữ nút **BOOTSEL**, cắm USB → hiện ổ `RP2350` → kéo file `.uf2` vào.
- Từ lần sau **không cần bấm nút**: gõ `eaa bootsel` trong console → Pico tự vào bootloader (~2 s) → kéo file mới vào. Lệnh này tắt watchdog trước; nếu không, watchdog reset chip ra khỏi bootloader (lỗi đã gặp trên phần cứng và đã sửa).
- LED GP25 nháy chậm = OK; nháy nhanh = SAFE_STOP (với bản bench/full thường là GP11 chưa nối GND).

### 10.3 Cổng USB của Pico
| Bản | CDC 0 | CDC 1 |
|---|---|---|
| full / bench | console + shell (máy này: COM7) | audio lệnh (COM6) |
| pctest | console + shell | **B-03 bridge** (thay cho UART0), audio tắt |

Ghi chú: thử dùng cổng CDC thứ 3 cho bridge thì máy tính **không gửi được dữ liệu xuống** (OUT endpoint không nhận) với USB stack cũ của Zephyr trên RP2350, nên bản pctest dùng CDC 1.

### 10.4 Phía Pi 3B
```bash
sudo raspi-config          # Interface > Serial: login shell = No, hardware = Yes
echo -e "enable_uart=1\ndtoverlay=disable-bt" | sudo tee -a /boot/firmware/config.txt
sudo systemctl disable hciuart; sudo reboot
pip3 install pyserial vosk
ls /dev/serial0 /dev/ttyACM*     # serial0 = B-03, ttyACM0 = console, ttyACM1 = audio
python3 hil_test.py --link /dev/serial0 --console /dev/ttyACM0 --audio /dev/ttyACM1
```
Nối vào orchestrator thật (PR nhỏ bên nhánh orchestrator, CH-200):
```python
import serial_asyncio  # pip install pyserial-asyncio
reader, writer = await serial_asyncio.open_serial_connection(url="/dev/serial0", baudrate=115200)
pipeline = OrchestratorPipeline(settings, transport=PipeTransport(reader, writer, max_frame_bytes=64))
```

### 10.5 Lệnh console
`eaa status` · `eaa run 150` · `eaa stop` · `eaa clear` · `eaa hb` · `eaa wake` · `eaa vstop` · `eaa jreset` · `eaa bootsel` · `eaa wdtest`

`eaa run` chỉ để test trên bàn: cần `eaa hb` trong vòng 300 ms, nếu không MCU tự SAFE_STOP (chính là luật heartbeat). `eaa wdtest` cố ý treo firmware để kiểm watchdog.

---

## 11. Quy trình test khi nối đủ phần cứng (theo milestone Jira)

| # | Test | Cách làm | Đạt khi | Trạng thái |
|---|---|---|---|---|
| T1 (M0) | Khởi động | nạp uf2, mở console | `eaa status` chạy | ✅ trên Pico thật |
| T2 (M2) | Link + safety tự động | `hil_test.py` | 30/30 | ✅ qua USB (pctest); ⏳ chạy lại qua UART thật với Pi |
| T3 (M2) | Link soak 10 phút | `link_test.py --soak 600` | ≈ 20 Hz, 0 frame lỗi | ⏳ (qua USB đã thấy 20.00 Hz, 0 lỗi) |
| T4 (M2) | Rút dây TX của Pi | rút GP1 khi đang chạy | SAFE_STOP + HEARTBEAT_TIMEOUT; cắm lại → HELLO lại | ⏳ cần dây |
| T5 (M3) | E-stop | gỡ jumper GP11 khi chạy | HARDWARE_ESTOP, PWM = 0 ngay; nối lại → ESTOP_RELEASED; `clear` → STOPPED | ⏳ (logic ✅ host test; đọc chân ✅: GP11 hở → "OPEN") |
| T6 (M3) | PWM không tải | `run 100`, đo GP2 | 20 kHz, duty ≈ 28%; EN = 1 | ⏳ cần đồng hồ/scope |
| T7 (M3) | Sensor | bản full, `eaa status` | dòng/nhiệt hợp lý; rút sensor → SENSOR_LOSS | ⏳ chưa có hàng |
| T8 (M4) | Fault injection vật lý | chắn beam > 3 s, nung MLX, quá tải motor | đúng event + reason | ⏳ (logic ✅ host test) |
| T9 (M5) | Jitter có motor + sensor + mic | `eaa jreset`, 10 phút, `eaa status` | ≤ 1000 µs | ✅ 129 µs khi chưa có sensor I2C/motor; ⏳ đo lại khi đủ |
| T10 (M6) | Audio | `audio_rx.py`, PTT, nói 5 s | WAV 5 s nghe rõ | ✅ kênh audio (80 128 mẫu, 0 lỗi); ⏳ mic (bỏ qua theo yêu cầu) |
| T11 (M6) | Voice stop không cần Pi | `eaa vstop` | băng dừng | ✅ logic + event trên Pico thật |
| T12 (M7) | Đầu-cuối | `voice_to_belt.py` + llama-server ABn | nói "stop the belt" → dừng | ⏳ |
| T13 | Watchdog | `eaa wdtest` | chip reset ≤ 0.6 s, boot lại | ✅ ~0.55 s |

Ghi số liệu với nhãn **"Pico 2 + Pi 3B (proxy)"** như quy ước ở CH-234.

---

## 12. Kết quả kiểm tra

### 12.1 Trên Pico 2 thật (2026-11-26, cắm USB vào PC, bản `pctest`)
`hil_test.py --link COM6 --console COM7 --jitter-seconds 60` → **30/30 đạt**:

| Nhóm | Kết quả đo |
|---|---|
| Bắt tay | HELLO → HELLO_ACK 656 ms (gồm thời gian mở cổng); sau đó STOPPED, flags 0x07 |
| Telemetry | **20.00 Hz**, 0 frame lỗi |
| Kiểm lệnh | 600 mm/s → LIMIT_EXCEEDED; 20 mm/s → BAD_PARAM; ttl 0 → EXPIRED; trùng `cmd_id` → DUPLICATE |
| RUN | CMD_ACK sau 62 ms; ramp tới 150 mm/s sau 0.61 s; PWM = 370‰ đúng công thức open-loop |
| Mất heartbeat | HEARTBEAT_TIMEOUT tới PC 328–360 ms sau heartbeat cuối (300 ms timeout + ≤ 10 ms tick an toàn + trễ USB/Windows); SAFE_STOP, tốc độ 0, lỗi chốt; RUN bị từ chối FAULT_LATCHED; CLEAR được sau khi heartbeat quay lại |
| HALT | SAFE_STOP, fault 0x0D |
| Gửi lại event | không ACK → 11 bản trong 1 s, cách nhau ~117 ms, cùng `event_seq`; ACK xong thì ngừng |
| Dữ liệu rác | frame sai CRC và COBS hỏng bị đếm và bỏ; link vẫn chạy |
| Giọng nói | wake → VOICE_EVENT sau 62 ms, cờ VOICE_ACTIVE bật/tắt đúng; "stop" → MCU tự dừng băng có ramp (0.88 s từ 200 mm/s) |
| **Jitter (NFR-01)** | vòng 100 Hz, wake word chạy mỗi 100 ms liên tục (VAD tắt): **max 207 µs** trong 60 s, thân vòng max 255 µs (lần đo cũ ở 200 Hz không có model: 129 µs) |
| **T1** | `eaa t1test` 20 lần khi băng chạy: phát hiện → EN tắt **13–22 µs** (mục 6.2) |
| Watchdog | `eaa wdtest` → chip reset sau ~0.55 s, boot lại bình thường |
| Kênh audio | `eaa wake` → 80 128 mẫu (5.01 s, 16 kHz) qua CDC, 0 frame lỗi (mẫu = 0 vì chưa cắm mic) |

Lỗi chỉ lộ ra khi chạy trên phần cứng, đã sửa: (1) `eaa bootsel` bị watchdog reset ra khỏi bootloader; (2) cổng CDC thứ 3 không nhận dữ liệu; (3) shell in nhiệt độ "chưa lắp" thành 3276.7 °C.

### 12.2 Trên máy tính
| Kiểm tra | Kết quả |
|---|---|
| Build full / bench / pctest (Pico 2) và unoq (STM32U585), Zephyr v4.2.0 | ✅ 0 warning |
| `b03_host_test` (CRC check value 0x29B1, vector vàng TELEMETRY, COBS 0–300 byte, lỗi CRC/magic/len) | ✅ |
| `cross_check.py`: C ↔ `codec.py`/`transport.py` của orchestrator, hai chiều | ✅ 29/29 trùng byte |
| `safety_host_test` (13 kịch bản) | ✅ |
| `link_test.py` ↔ `MockMcu` thật của orchestrator qua TCP | ✅ |
| Wake word "Hey Edgie" (TFLM) | ✅ bit-exact 14/14, HIL 30/30 khi model chạy liên tục (WAKEWORD_REPORT.md) |

---

## 13. Port sang UNO Q (STM32U585): đã build được

### 13.1 Vì sao phải port dù cùng lõi Cortex-M33
Lõi CPU giống nhau nên logic C (protocol, safety, control) chạy y hệt. Nhưng **mọi thứ quanh lõi khác nhau**, và chính những chỗ đó quyết định an toàn và thời gian thực:

| Khía cạnh | Pico 2 (RP2350) | UNO Q (STM32U585) | Ảnh hưởng |
|---|---|---|---|
| Timer hệ thống | timer 1 MHz | board chính thức dùng **LPTIM1 32 kHz → 4096 tick/s** | 5 ms = 20.48 tick → làm tròn thành 21 tick = 5.13 ms: vòng chạy ~195 Hz, lệch ~244 µs mỗi chu kỳ, bộ đếm cycle chỉ phân giải ~30 µs. **Đã sửa:** SysTick 160 MHz, 10 000 tick/s (5 ms = đúng 50 tick; chu kỳ 10 ms hiện tại = đúng 100 tick). |
| Watchdog | tắt được (cần cho `bootsel`) | IWDG **không tắt được** sau khi bật | `eaa bootsel` chỉ có trên RP2350 |
| PWM | PWM slice 1 | TIM3 CH3/CH4 (PB0 = D3, PB1 = D6) | chỉ đổi devicetree |
| Link MPU | UART0 GP0/GP1 | **LPUART1 PG7/PG8 (có RTS/CTS)** nối nội bộ tới QRB2210 | phía Linux là `/dev/ttyHS1`, đúng `uart_device` trong config orchestrator |
| Console | USB CDC | USART1 trên D0/D1 | |
| Mic | PIO I2S (tự viết) | SAI, chưa làm; cần kiểm chân trên JMISC (CH-103) | voice tắt trong bản UNO Q |
| Encoder | ngắt GPIO | ngắt GPIO (EXTI) chạy được; tốt hơn là timer encoder mode | |
| Flash layout | chạy từ 0x10000000 | board chính thức đặt app ở slot0 **0x08010000** (64 KB đầu cho bootloader) | khi nạp phải giữ bootloader của Arduino hoặc đổi layout |
| Tài nguyên | Flash 86 KB, RAM 95 KB / 520 KB | **Flash 78 KB / 2 MB, RAM 32 KB / 768 KB** (không có USB/mic) | dư nhiều cho model wake word |

Lỗi port phát hiện khi build: lệnh shell `wake`/`vstop` gọi module voice cả khi voice bị tắt → lỗi link. Đã sửa.

### 13.2 Đã làm
- `firmware/boards/arduino/uno_q/`: định nghĩa board **chính thức từ Zephyr main** (Arduino SA, Apache-2.0), chỉnh tối thiểu cho v4.2: bỏ LED matrix, đổi khai báo partition, thêm thuộc tính ADC theo binding v4.2. Xoá thư mục này khi project lên bản Zephyr có sẵn `arduino_uno_q`.
- `firmware/boards/arduino_uno_q.overlay` + `.conf`: cùng tên thuộc tính với Pico → **không phải đổi dòng C nào** (trừ lỗi ở trên).

| Chức năng | Pico 2 | UNO Q header | STM32U585 |
|---|---|---|---|
| Link B-03 | GP0/GP1 | (nội bộ) | LPUART1 PG7/PG8 |
| RPWM / LPWM | GP2 / GP3 | D3 / D6 | PB0 / PB1 (TIM3 CH3/CH4) |
| EN | GP4 | D8 | PB4 |
| Encoder A/B | GP6/GP7 | A2/A3 | PA6/PA7 |
| I2C SDA/SCL | GP8/GP9 | SDA/SCL | PB11/PB10 (I2C2, 100 kHz) |
| Cảm biến quang | GP10 | D2 | PB3 |
| E-stop | GP11 | D7 | PB2 |
| LED nghe / PTT | GP15 / GP16 | D9 / D10 | PB8 / PB9 |
| LED heartbeat | GP25 | LED3 xanh | PH11 |

### 13.3 Còn lại khi có UNO Q
1. Xác nhận LPUART1 đúng là đường nối tới QRB2210, và tốc độ/flow control (board file nói vậy, cần đo).
2. Chọn đi **raw UART (Profile B, như bây giờ)** hay qua **Arduino Bridge / `arduino-router` (Profile A)**. Profile B nghĩa là đường điều khiển không đi qua router của Arduino.
3. Cách nạp: OpenOCD/STM32CubeProgrammer qua SWD hoặc công cụ của Arduino; giữ bootloader ở 0x08000000.
4. Driver mic bằng Zephyr I2S/SAI theo `voice/audio_in.h`; kênh audio thứ 2 (UART ≥ 921600 hoặc SPI).
5. Chạy lại `hil_test.py` trên UNO Q và so với Pico 2.

---

## 14. Bản đồ file

```
embedded/
├── pico2/
│   ├── PICO2_PLAN.md          kế hoạch ban đầu (CH-236)
│   ├── REPORT.md              báo cáo này
│   ├── release/               eaa_mcu_pico2_{full,bench,pctest}.uf2, eaa_mcu_unoq.bin
│   ├── tools/setup_zephyr.sh  cài toolchain 1 lần
│   └── firmware/
│       ├── CMakeLists.txt, prj.conf, Kconfig, build.ps1
│       ├── boards/rpi_pico2_rp2350a_m33.overlay|.conf   ← pin map Pico 2
│       ├── boards/pc_test.overlay                       ← bản test trên PC
│       ├── boards/arduino_uno_q.overlay|.conf           ← pin map UNO Q
│       ├── boards/arduino/uno_q/                        ← board UNO Q (từ Zephyr main)
│       ├── src/main.c          khởi động + shell
│       ├── src/eaa.h           giao diện các module
│       ├── src/safety.c        FSM an toàn
│       ├── src/control.c       vòng 100 Hz
│       ├── src/motor.c, encoder.c, sensors.c, bridge.c
│       ├── src/proto/          b03.c, cobs.c, crc16.c (C thuần)
│       ├── src/voice/          voice.c, i2s_in_rp2.c, audio_out.c, kws_stub.c, kws_ei.cpp
│       └── tests/host/         b03_host_test.c, safety_host_test.c, cross_check.py, shim/
└── pi/                         b03.py, link_test.py, hil_test.py, audio_rx.py, voice_to_belt.py
```
