# Pico 2 (RP2350) — Hướng triển khai MCU tạm thời thay STM32U585

> **Đã triển khai:** xem [REPORT.md](REPORT.md) (firmware, đấu dây, nạp, test). Khác với kế hoạch dưới đây: chỉ dùng 1 core cho sát STM32U585; mic là I2S (INMP441) GP12–14.

> Nhánh: `embedded` · Jira: CH-235 (proposal, Done), CH-236 (Raspberry Pico Implementation, To Do), CH-103 (wake word embedded side), CH-39 (Embedded Implementation)
> Cập nhật: 2026-11-25 · Tất cả số liệu timing dưới đây là **mục tiêu**, chưa đo trên board.

## 0. Bối cảnh (tóm tắt từ Jira + code)

- UNO Q chưa có → **Pi 3B** đóng vai MPU (Linux: SLM ABn, STT/TTS, orchestrator FSM + Safety Arbiter) (CH-234), **Pico 2** đóng vai MCU (motor, sensor, safety, wake word) (CH-235).
- Quy tắc an toàn cốt lõi: **motor phải an toàn kể cả khi phía AI chậm / sai / treo.** Pico tự quyết safe-stop, không chờ Pi.
- E-stop vật lý + cắt nguồn motor là **phần cứng**, độc lập cả 2 board (Category 0, CH-83 #7).
- Firmware viết trên **Zephyr** (`rpi_pico2`), tách code board-specific ra → sau này port sang STM32U585 chỉ đổi devicetree / pin map.
- Phía Pi đã có sẵn ở nhánh `orchestrator` (`src/control_pipeline/`): codec B-03 v1.1, `McuBridgeWorker`, `MockMcu`, `PipeTransport` (COBS). **Firmware Pico phải khớp byte-by-byte với `codec.py` + `mock_mcu.py`** — đó là spec thực tế.

```
Pi 3B (Linux)                                    Pico 2 (Zephyr)
 orchestrator FSM + Arbiter                        safety FSM (BOOT/STOPPED/RUNNING/SAFE_STOP)
 McuBridgeWorker  ──UART0 115200, COBS+B-03──▶     bridge thread
 STT container   ◀──USB CDC (audio 16k/16bit)──    mic + wake word (KWS)
                                                   100 Hz control loop ─▶ BTS7960 ─▶ motor 24 V
                                                   INA226, MLX90614 (I2C), E18 PE, E-stop aux, encoder
```

## 1. Protocol phải implement (trích từ `orchestrator:src/control_pipeline/codec.py`)

### 1.1 Lớp vận chuyển (UART)
- UART 115200 8N1, 3.3 V, không flow control.
- Mỗi frame được **COBS-encode + byte `0x00` làm delimiter** (y hệt `PipeTransport` — Profile B).
- Frame giải mã tối đa 12 + 40 + 2 = **54 byte** → buffer RX 64 byte là đủ; frame dài hơn → bỏ.

### 1.2 Frame B-03 (little-endian)
| Off | Len | Field |
|---|---|---|
| 0 | 1 | magic `0xAA` |
| 1 | 1 | msg_type |
| 2 | 2 | seq (u16) |
| 4 | 2 | ack (u16, seq cuối nhận từ peer) |
| 6 | 4 | tick_ms (u32) |
| 10 | 1 | flags |
| 11 | 1 | length (≤ 40) |
| 12 | N | payload |
| 12+N | 2 | CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, không reflect, không XOR) trên byte 0..12+N-1 |

### 1.3 Message
MPU → MCU (Pico phải **parse**):
| Type | Tên | Payload |
|---|---|---|
| 0x01 | HELLO | `u8 proto_ver, u32 session_id` |
| 0x02 | HEARTBEAT | `u8 mpu_state` |
| 0x03 | EVENT_ACK | `u16 event_seq` |
| 0x10 | RUN | `u16 cmd_id, u16 ttl_ms, u8 kind, u16 setpoint_mms` |
| 0x11 | SET_SPEED | `u16 cmd_id, u16 ttl_ms, u16 setpoint_mms` |
| 0x12 | STOP | `u16 cmd_id, u16 ttl_ms, u8 kind` (0 STOP, 1 PAUSE) |
| 0x13 | HALT | `u16 cmd_id, u16 ttl_ms` |
| 0x14 | CLEAR_FAULT | `u16 cmd_id, u16 ttl_ms` |

MCU → MPU (Pico phải **gửi**):
| Type | Tên | Payload |
|---|---|---|
| 0x81 | HELLO_ACK | `u8 proto_ver, u16 mcu_boot_id, u16 fw_ver, u8 mcu_state, u8 fault_reason` (7 B) |
| 0x90 | TELEMETRY (20 Hz) | `u8 mcu_state, u8 fault_reason, u16 setpoint_mms, u16 speed_mms, u16 current_ma, i16 temp_c10, u8 sensor_status, u16 pwm_pm` (13 B) |
| 0x91 | MCU_EVENT | `u8 event_kind, u8 reason, u16 event_seq, u8 mcu_state, i16 observed, i16 threshold` (9 B) — gửi lại tới khi có EVENT_ACK |
| 0x92 | CMD_ACK | `u16 cmd_id, u8 result (0 ACCEPTED/1 REJECTED/2 DUPLICATE), u8 reason, u8 mcu_state` |
| 0x93 | VOICE_EVENT | `u8 kind (0 WAKE/1 STOP), u8 confidence_pct, u16 event_seq, u8 mcu_state, u16 model_ver` — gửi lại tới khi có EVENT_ACK |

- `McuState`: 0 BOOT, 1 STOPPED, 2 RUNNING, 3 SAFE_STOP.
- `flags` (header, mọi frame MCU→MPU): bit0 ESTOP_CLOSED, bit1 PE_CLEAR, bit2 HB_VALID, bit3 FAULT_LATCHED, bit4 MOTOR_ENABLED, bit5 SENSORS_OK, bit6 VOICE_ACTIVE.
- `event_kind` (MCU_EVENT): 0x01 SAFE_STOP, 0x02 MOTOR_OVERLOAD, 0x03 JAM_DETECTED, 0x04 HEARTBEAT_TIMEOUT, 0x05 HARDWARE_ESTOP, 0x06 ESTOP_RELEASED, 0x07 OVERTEMP, 0x08 SENSOR_LOSS, 0x09 FAULT_CLEARED, 0x0A STATE_CHANGED, 0x0B BOOT, 0x0C VOICE_WAKE, 0x0D VOICE_STOP.
- `ReasonCode` phía MCU (0x00–0x0F): 0x00 OK, 0x01 ESTOP_ACTIVE, 0x02 PHOTOELECTRIC_BLOCKED, 0x03 MOTOR_OVERCURRENT, 0x04 MOTOR_OVERTEMP, 0x05 HEARTBEAT_TIMEOUT, 0x06 STATE_NOT_PERMITTED, 0x07 SENSOR_LOSS, 0x08 BAD_PARAM, 0x09 EXPIRED, 0x0A BUSY, 0x0B UNKNOWN_CMD, 0x0C LIMIT_EXCEEDED, 0x0D MPU_HALT_REQUEST, 0x0E FAULT_LATCHED, 0x0F NOT_SYNCED.
- `temp_c10`: `0x7FFF` = chưa lắp sensor, `0x7FFE` = sensor lỗi.

### 1.4 Timer (khớp `config_example.yaml`)
| Thông số | Giá trị | Bên nào |
|---|---|---|
| Heartbeat MPU→MCU | 100 ms | Pi gửi |
| **Watchdog heartbeat trên Pico** | **300 ms → SAFE_STOP + event 0x04** | Pico |
| Telemetry | 50 ms (20 Hz) | Pico |
| Link STALE / LOST trên Pi | 250 / 500 ms | Pi |
| HELLO retry | 500 ms | Pi |
| Command TTL mặc định | 500 ms (quá hạn → REJECTED, ERR_EXPIRED) | Pico kiểm |
| Event retransmit | đề xuất 100 ms, tối đa ~20 lần | Pico (cần chốt với team, mock chưa có) |

### 1.5 Golden test vector (dùng cho unit test firmware)
TELEMETRY từ spec §9 (phải decode ra RUNNING, setpoint 120, speed 118, current 850, temp 342, flags 0x37):
```
aa 90 28 00 02 00 88 13 00 00 37 0d 02 00 78 00 76 00 52 03 56 01 80 76 00 17 6f
```
Chạy `python -m control_pipeline.codec` (nhánh orchestrator) để in thêm vector HEARTBEAT, RUN.

## 2. Pin map đề xuất (Pico 2 ↔ Pi 3B ↔ ngoại vi)

| Chức năng | Pico 2 GPIO | Nối tới | Ghi chú |
|---|---|---|---|
| UART0 TX | GP0 | Pi GPIO15 RXD (pin 10) | link điều khiển B-03 |
| UART0 RX | GP1 | Pi GPIO14 TXD (pin 8) | |
| GND chung | GND | Pi GND (pin 6), GND driver, GND buck | **bắt buộc chung mass** |
| BTS7960 RPWM | GP2 (PWM1A) | IBT-2 RPWM | ~20 kHz, chạy 1 chiều |
| BTS7960 LPWM | GP3 (PWM1B) | IBT-2 LPWM | giữ 0 nếu belt 1 chiều |
| BTS7960 R_EN + L_EN | GP4 | IBT-2 R_EN, L_EN | **có pull-down ngoài 10 k** → mặc định tắt khi Pico reset |
| Encoder A / B | GP6 / GP7 | encoder | PIO quadrature (2 chân liên tiếp). Đang bị chặn bởi CH-107 |
| I2C0 SDA / SCL | GP8 / GP9 | INA226 + MLX90614 (3.3 V) | 100 kHz (MLX90614 SMBus). Pull-up 4.7 k lên 3V3 |
| Photoelectric E18-D80NK | GP10 | output NPN | pull-up lên **3.3 V** (không phải 5 V) |
| E-stop aux contact (NC) | GP11 | tiếp điểm phụ | pull-up; mở = E-stop. Mạch cắt nguồn motor vẫn là phần cứng |
| Mic PDM CLK / DATA | GP12 / GP13 | MEMS PDM mic | qua PIO. Hoặc I2S (INMP441): SCK GP12, WS GP13, SD GP14 |
| LED "listening" / status | GP15 (+ LED onboard GP25) | LED | CH-103 yêu cầu báo trạng thái ≤ 1 s |
| USB | micro-USB | Pi 3B USB | **USB CDC kênh audio** + log; cấp nguồn xem mục 3 |

Đổi INA226 sang I2C nên **không cần ADC** cho dòng điện → hết lo vụ 12-bit / chỉ 3 ADC trong CH-235. ADC (GP26–28) để dự phòng.

> ⚠️ RP2350 có erratum **E9** (chân input bật pull-down nội có thể bị giữ ~2 V). Đừng dựa vào pull-down nội — dùng **điện trở ngoài**, nhất là cho chân EN driver.

## 3. Cấu hình phía Pi 3B

1. `/boot/firmware/config.txt`: `enable_uart=1` + `dtoverlay=disable-bt` (đưa PL011 về GPIO14/15, bỏ mini-UART lệch baud).
2. Tắt serial console: `sudo raspi-config` → Interface → Serial: login shell **No**, hardware **Yes**. Thiết bị: `/dev/serial0` (→ `ttyAMA0`).
3. Bridge: `PipeTransport` nhận `StreamReader/StreamWriter` → bọc bằng `pyserial-asyncio`:
   ```python
   import serial_asyncio
   from control_pipeline.bridge.transport import PipeTransport
   reader, writer = await serial_asyncio.open_serial_connection(url="/dev/serial0", baudrate=115200)
   transport = PipeTransport(reader, writer, max_frame_bytes=64)
   pipeline = OrchestratorPipeline(settings, transport=transport)
   ```
   `OrchestratorPipeline` đã nhận `transport=` inject; nhưng `transport_profile: "pipe"` trong config **chưa được nối** → cần PR nhỏ bên nhánh `orchestrator` (báo Huy Vu, CH-200). Chỉnh `uart_device` từ `/dev/ttyHS1` (UNO Q) thành `/dev/serial0`.
4. Nguồn: Pico cấp từ buck 5 V vào **VSYS** (qua diode nếu đồng thời cắm USB), hoặc lấy luôn từ USB của Pi trong giai đoạn bench. Không lấy từ rail 24 V trực tiếp.

## 4. Kiến trúc firmware (Zephyr)

```
embedded/pico2/firmware/
  CMakeLists.txt, prj.conf
  boards/rpi_pico2_rp2350a_m33.overlay   # pin map, uart0, pwm, i2c0, pio
  src/
    main.c
    proto/   b03_codec.[ch]  cobs.[ch]  crc16.[ch]   # thuần C, không phụ thuộc board → unit test trên native_sim
    bridge/  bridge.c        # RX parse, TX queue, HELLO/HB watchdog, retransmit event
    safety/  safety_fsm.c    # BOOT/STOPPED/RUNNING/SAFE_STOP, latch fault, check invariant
    control/ motor.c  speed_loop.c  encoder_pio.c
    sensors/ ina226.c  mlx90614.c  pe_estop.c
    voice/   mic_pio.c  kws.cpp (Edge Impulse)  audio_usb.c
  tests/proto/                            # ztest: golden vector, COBS round-trip, CRC
```

Thread / ưu tiên (Zephyr: số nhỏ = ưu tiên cao):
| Thread | Chu kỳ | Prio | Việc |
|---|---|---|---|
| `control` | 10 ms (100 Hz, `k_timer`) | cao nhất (coop) | ramp setpoint, ghi PWM (open-loop). Cắt motor khi có sự cố **không** chờ vòng này: ngắt E-stop và safety trip cắt EN trực tiếp (REPORT.md §6.1–6.2) |
| `safety` | event + 10 ms | cao | FSM, latch fault, HB watchdog 300 ms, task watchdog phần cứng RP2350 |
| `sensors` | 20–50 ms | trung | INA226, MLX90614 (I2C chậm, **không** chạy trong control loop) |
| `bridge` | event-driven | trung | COBS RX/TX, CMD_ACK, telemetry 20 Hz, retransmit |
| `kws` | 250 ms stride | thấp | inference wake word / "stop" |
| `audio_usb` | khi WAKE | thấp | stream ≤ 5 s audio qua USB CDC |

Nguyên tắc:
- **Safety không phụ thuộc bridge**: mất link → watchdog tự SAFE_STOP. Bridge chỉ đọc state, gửi lệnh qua message queue vào safety FSM; safety FSM là nơi duy nhất được ghi `motor_enable`.
- Pico **tự validate lại** mọi lệnh (state cho phép, setpoint trong [50, 500] mm/s, TTL chưa hết, `cmd_id` trùng → DUPLICATE). Không tin Arbiter phía Pi.
- SAFE_STOP là **latched**: chỉ thoát bằng CLEAR_FAULT khi nguyên nhân đã hết (E-stop đã nhả, dòng/nhiệt bình thường).
- Hardware watchdog RP2350 (~500 ms) feed từ `safety` thread — nếu firmware treo, chip reset, EN driver có pull-down ngoài nên motor tắt.
- ⚠️ Cần kiểm: Zephyr cho `rpi_pico2` hiện chạy **1 core** (core 1 chưa dùng được dễ dàng). Ý "core 1 cho link" trong CH-235 có thể chưa khả thi → dùng thread priority thay thế, ghi lại trong report.
- ⚠️ Zephyr chưa có driver PIO quadrature / PDM cho RP2350 sẵn → phải viết PIO program (dùng `hal_rpi_pico` + `pioasm`). Fallback encoder: GPIO interrupt (đủ cho tốc độ belt thấp).

## 5. Lộ trình (milestone → Jira)

Mỗi milestone có "xong khi" rõ ràng. Đã tạo task trên Jira dưới epic **CH-39** (tổng hợp ở CH-236):
M0 = CH-237 · M1 = CH-238 · M2 = CH-239 · M3 = CH-240 · M4 = CH-241 · M5 = CH-242 · M6 = CH-243 · M7 = CH-244.

### M0 — Toolchain + blinky (0.5 ngày)
- Cài Zephyr SDK + `west` (Windows hoặc WSL), `west init` Zephyr ≥ 4.1.
- `west build -b rpi_pico2/rp2350a/m33 samples/basic/blinky`, nạp bằng kéo `.uf2` (giữ BOOTSEL) hoặc debug probe SWD.
- ✅ Xong khi: LED nháy, `printk` ra USB CDC console.

### M1 — Protocol lib + unit test (1–2 ngày) — *làm được ngay, không cần phần cứng*
- Port `crc16`, `cobs`, `b03_codec` sang C, test bằng ztest trên `native_sim`.
- ✅ Xong khi: decode đúng golden TELEMETRY vector, encode HEARTBEAT/RUN trùng byte với `codec.py`, COBS round-trip.

### M2 — UART bridge với Pi (2–3 ngày) — *chỉ cần Pico + Pi + 3 dây*
- Pico chạy "mock MCU bằng C": HELLO_ACK, TELEMETRY 20 Hz giả, CMD_ACK, watchdog 300 ms, event + retransmit.
- Phía Pi: script test dùng `McuBridgeWorker` + `PipeTransport` qua `/dev/serial0`.
- ✅ Xong khi: link UP ổn định ≥ 10 phút, `bad_crc = 0`; rút dây TX của Pi → Pico vào SAFE_STOP ≤ 300 ms + gửi HEARTBEAT_TIMEOUT; cắm lại → HELLO lại được.
- Đây là milestone giá trị nhất cho Alpha demo (CH-147): orchestrator thật ↔ MCU thật.

### M3 — Bring-up ngoại vi (2–3 ngày, phụ thuộc hàng về — CH-83)
- PWM → BTS7960 (đang giao), test motor không tải ở duty thấp. E-stop aux + E18 input. INA226, MLX90614 qua I2C.
- ✅ Xong khi: telemetry có số thật (current_ma, temp_c10, sensor_status, flags).

### M4 — Safety FSM thật (2 ngày)
- Thay mock bằng FSM: E-stop, jam (PE bị chặn quá X ms khi RUNNING), overcurrent > 2500 mA, overtemp > 80 °C, link timeout, sensor loss (I2C NACK).
- ✅ Xong khi: bộ fault-injection pass (rút sensor, giữ E-stop, chặn PE, kill orchestrator) — mỗi case → SAFE_STOP + đúng event + đúng reason.

### M5 — Speed control 100 Hz, open-loop (encoder không thuộc phase này, CH-107; lý do bỏ 200 Hz: REPORT.md §6.1)
- Có encoder: PIO quadrature + PI loop, đo jitter bằng GPIO toggle + logic analyzer.
- Chưa có encoder: open-loop setpoint → PWM theo bảng hiệu chuẩn (đo mm/s bằng tay), `speed_mms` = ước lượng, ghi rõ trong report.
- ✅ Xong khi: jitter control loop ≤ 1 ms (NFR-01).

### M6 — Wake word + audio (CH-103, 4–6 ngày)
- Mic PDM/I2S qua PIO @ 16 kHz. Edge Impulse export C++ library (3 lớp: wake / stop / noise-other, cửa sổ 1 s, stride 250 ms).
- "stop" → Pico tự dừng belt (không cần wake word, không cần Pi) + VOICE_EVENT kind STOP.
- WAKE → set flag VOICE_ACTIVE, LED, VOICE_EVENT kind WAKE, stream ≤ 5 s audio qua **USB CDC** (12 Mbps ≫ 32 kB/s cần) → container STT trên Pi.
- ✅ Xong khi: đo lại jitter M5 khi KWS + mic đang chạy vẫn ≤ 1 ms; kill Pi → nói "stop" vẫn dừng belt.

### M7 — End-to-end + port note
- Giọng nói → STT → SLM ABn → orchestrator/FSM → UART → Pico → motor; chạy lại fault-injection.
- Viết "porting note" Pico 2 → STM32U585: những gì khác (PIO → timer encoder mode, USB CDC → kênh audio UNO Q, UART → Arduino Bridge/`arduino-router`).

## 6. Rủi ro & quyết định cần chốt với team

| # | Vấn đề | Đề xuất |
|---|---|---|
| 1 | Kênh audio: CH-103 nói UART2 ≥ 921600 hoặc SPI | Trên Pico dùng **USB CDC** (đơn giản, nhanh). UART1 921600 là phương án dự phòng. Ghi decision note. |
| 2 | Retransmit interval / max retry của event chưa có trong spec | Đề xuất 100 ms / 20 lần, thêm vào B-03 v1.2 |
| 3 | `transport_profile: pipe` chưa nối trong orchestrator | PR nhỏ bên nhánh `orchestrator` (CH-200) |
| 4 | Zephyr single-core trên RP2350 | Dùng thread priority; nếu jitter > 1 ms mới tính Pico SDK cho core 1 |
| 5 | Encoder bị chặn (CH-107) | M5 chạy open-loop trước, không chặn M2–M4 |
| 6 | Hàng chưa về: BTS7960, E-stop, E18, INA226, MLX90614, mic (CH-83) | M0–M2 không cần hàng → làm ngay |
| 7 | E9 erratum RP2350 | Pull-up/pull-down ngoài cho mọi input an toàn |

## 7. Việc làm ngay tuần này
1. M0 + M1 (không cần phần cứng).
2. Nối Pico ↔ Pi 3 dây, làm M2 → có demo "orchestrator thật nói chuyện MCU thật".
3. Chốt với team mục 6.1–6.3.
4. Đặt mua nốt: mic PDM/I2S (ví dụ INMP441 hoặc SPH0645), INA226, MLX90614 3.3 V, E18-D80NK, E-stop.
