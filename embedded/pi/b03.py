"""Standalone B-03 v1.1 codec + COBS for bench tools on the Pi (no pydantic needed).

Byte-identical to orchestrator:src/control_pipeline/codec.py and bridge/transport.py
(checked by `python b03.py --selftest [path/to/orchestrator/src]`). The real system uses
the orchestrator's McuBridgeWorker; this module is only for hardware bring-up tests.
"""
from __future__ import annotations

import struct
import sys

MAGIC = 0xAA
# MPU -> MCU
HELLO, HEARTBEAT, EVENT_ACK = 0x01, 0x02, 0x03
RUN, SET_SPEED, STOP, HALT, CLEAR_FAULT = 0x10, 0x11, 0x12, 0x13, 0x14
# MCU -> MPU
HELLO_ACK, TELEMETRY, MCU_EVENT, CMD_ACK, VOICE_EVENT = 0x81, 0x90, 0x91, 0x92, 0x93

STATES = {0: "BOOT", 1: "STOPPED", 2: "RUNNING", 3: "SAFE_STOP"}
FLAGS = ["ESTOP_CLOSED", "PE_CLEAR", "HB_VALID", "FAULT_LATCHED", "MOTOR_ENABLED", "SENSORS_OK", "VOICE_ACTIVE"]
EVENTS = {1: "SAFE_STOP", 2: "MOTOR_OVERLOAD", 3: "JAM_DETECTED", 4: "HEARTBEAT_TIMEOUT", 5: "HARDWARE_ESTOP",
          6: "ESTOP_RELEASED", 7: "OVERTEMP", 8: "SENSOR_LOSS", 9: "FAULT_CLEARED", 10: "STATE_CHANGED",
          11: "BOOT", 12: "VOICE_WAKE", 13: "VOICE_STOP"}
REASONS = {0x00: "OK", 0x01: "ESTOP_ACTIVE", 0x02: "PHOTOELECTRIC_BLOCKED", 0x03: "MOTOR_OVERCURRENT",
           0x04: "MOTOR_OVERTEMP", 0x05: "HEARTBEAT_TIMEOUT", 0x06: "STATE_NOT_PERMITTED", 0x07: "SENSOR_LOSS",
           0x08: "BAD_PARAM", 0x09: "EXPIRED", 0x0A: "BUSY", 0x0B: "UNKNOWN_CMD", 0x0C: "LIMIT_EXCEEDED",
           0x0D: "MPU_HALT_REQUEST", 0x0E: "FAULT_LATCHED", 0x0F: "NOT_SYNCED"}
ACK_RESULT = {0: "ACCEPTED", 1: "REJECTED", 2: "DUPLICATE"}


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def encode(msg_type: int, seq: int, ack: int, tick_ms: int, flags: int, payload: bytes) -> bytes:
    body = struct.pack("<BBHHIBB", MAGIC, msg_type, seq & 0xFFFF, ack & 0xFFFF, tick_ms & 0xFFFFFFFF,
                       flags & 0xFF, len(payload)) + payload
    return body + struct.pack("<H", crc16(body))


def decode(data: bytes):
    if len(data) < 14 or data[0] != MAGIC:
        return None
    n = data[11]
    if n > 40 or len(data) < 14 + n:
        return None
    if crc16(data[:12 + n]) != struct.unpack_from("<H", data, 12 + n)[0]:
        return None
    _, t, seq, ack, tick, flags, _ = struct.unpack_from("<BBHHIBB", data)
    return t, seq, ack, tick, flags, bytes(data[12:12 + n])


def cobs_encode(data: bytes) -> bytes:
    out, idx, code = bytearray([0]), 0, 1
    for b in data:
        if b == 0:
            out[idx] = code
            idx, code = len(out), 1
            out.append(0)
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[idx] = code
                idx, code = len(out), 1
                out.append(0)
    out[idx] = code
    return bytes(out)


def cobs_decode(data: bytes):
    out, i = bytearray(), 0
    while i < len(data):
        code = data[i]
        if code == 0:
            return None
        i += 1
        end = i + code - 1
        if end > len(data):
            return None
        out += data[i:end]
        i = end
        if code < 0xFF and i < len(data):
            out.append(0)
    return bytes(out)


# ---- payload helpers --------------------------------------------------------
def p_hello(session_id: int) -> bytes:
    return struct.pack("<BI", 1, session_id & 0xFFFFFFFF)


def p_heartbeat(mpu_state: int) -> bytes:
    return struct.pack("<B", mpu_state)


def p_event_ack(event_seq: int) -> bytes:
    return struct.pack("<H", event_seq)


def p_run(cmd_id: int, ttl: int, kind: int, sp: int) -> bytes:
    return struct.pack("<HHBH", cmd_id, ttl, kind, sp)


def p_set_speed(cmd_id: int, ttl: int, sp: int) -> bytes:
    return struct.pack("<HHH", cmd_id, ttl, sp)


def p_stop(cmd_id: int, ttl: int, kind: int = 0) -> bytes:
    return struct.pack("<HHB", cmd_id, ttl, kind)


def p_cmd(cmd_id: int, ttl: int) -> bytes:  # HALT, CLEAR_FAULT
    return struct.pack("<HH", cmd_id, ttl)


def describe(t: int, flags: int, p: bytes) -> str:
    fl = ",".join(name for i, name in enumerate(FLAGS) if flags & (1 << i)) or "-"
    if t == TELEMETRY and len(p) >= 13:
        st, fr, sp, spd, cur, temp, ss, pwm = struct.unpack_from("<BBHHHhBH", p)
        tstr = "n/a" if temp == 0x7FFF else ("FAULT" if temp == 0x7FFE else f"{temp / 10:.1f}C")
        return (f"TELEM {STATES.get(st, st)} fault={REASONS.get(fr, fr)} sp={sp} speed={spd}mm/s "
                f"I={cur}mA T={tstr} ss=0x{ss:02x} pwm={pwm} [{fl}]")
    if t == HELLO_ACK and len(p) >= 7:
        pv, boot, fw, st, fr = struct.unpack_from("<BHHBB", p)
        return f"HELLO_ACK proto={pv} boot=0x{boot:04x} fw=0x{fw:04x} {STATES.get(st, st)} fault={REASONS.get(fr, fr)}"
    if t == CMD_ACK and len(p) >= 5:
        cid, res, rs, st = struct.unpack_from("<HBBB", p)
        return f"CMD_ACK id={cid} {ACK_RESULT.get(res, res)} reason={REASONS.get(rs, rs)} state={STATES.get(st, st)}"
    if t == MCU_EVENT and len(p) >= 9:
        k, rs, es, st, obs, thr = struct.unpack_from("<BBHBhh", p)
        return f"EVENT {EVENTS.get(k, k)} reason={REASONS.get(rs, rs)} seq={es} state={STATES.get(st, st)} obs={obs} thr={thr}"
    if t == VOICE_EVENT and len(p) >= 7:
        k, conf, es, st, mv = struct.unpack_from("<BBHBH", p)
        return f"VOICE {'WAKE' if k == 0 else 'STOP'} conf={conf}% seq={es} state={STATES.get(st, st)} model=0x{mv:04x}"
    return f"type=0x{t:02x} len={len(p)}"


def _selftest(orch_src: str | None) -> int:
    golden = bytes.fromhex("aa90280002008813000037 0d0200780076005203560180760017 6f".replace(" ", ""))
    d = decode(golden)
    assert d and d[0] == TELEMETRY and d[4] == 0x37, "golden vector"
    for data in (b"", b"\x00", b"\x11\x00\x22", bytes(range(1, 256)) * 2):
        assert cobs_decode(cobs_encode(data)) == data
    if orch_src:
        sys.path.insert(0, orch_src)
        from control_pipeline import codec  # type: ignore
        from control_pipeline.bridge import transport  # type: ignore
        assert encode(RUN, 2, 1, 2000, 0, p_run(1, 500, 0, 300)) == codec.encode_run(2, 1, 2000, 1, 500, 0, 300)
        assert encode(HEARTBEAT, 1, 0, 1000, 0, p_heartbeat(1)) == codec.encode_heartbeat(1, 0, 1000, 1)
        assert encode(HELLO, 9, 8, 7, 0, p_hello(0xCAFE)) == codec.encode_hello(9, 8, 7, 0xCAFE)
        for data in (b"\x00", b"\x11\x00\x22\x00\x00\x33", bytes(((i % 255) + 1) for i in range(300))):
            assert cobs_encode(data) == transport.cobs_encode(data)
            assert cobs_decode(cobs_encode(data)) == transport.cobs_decode(transport.cobs_encode(data))
        print("b03.py matches orchestrator codec.py / transport.py")
    print("b03.py selftest passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(_selftest(sys.argv[2] if len(sys.argv) > 2 else None))
