"""Cross-check the firmware C protocol code against the orchestrator Python codec.

Usage (from this folder):
    python cross_check.py <path-to-b03_host_test-binary> <path-to-orchestrator/src>

<path-to-orchestrator/src> is the `src` folder of branch `orchestrator`, e.g.
    git worktree add ../orch origin/orchestrator   ->   ../orch/src
Exit code 0 = C and Python agree byte for byte in both directions.
"""
from __future__ import annotations

import subprocess
import sys

binary, orch_src = sys.argv[1], sys.argv[2]
sys.path.insert(0, orch_src)

from control_pipeline import codec  # noqa: E402
from control_pipeline.bridge.transport import cobs_encode  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f"  {detail}" if detail and not ok else ""))
    fails += 0 if ok else 1


# ---- 1. MCU -> MPU: C encodes, Python decodes / re-encodes -----------------
out = subprocess.run([binary, "enc"], capture_output=True, text=True, check=True).stdout
c = dict(line.split(" ", 1) for line in out.strip().splitlines())
c = {k: bytes.fromhex(v.strip()) for k, v in c.items()}

dec = codec.decode_frame(c["hello_ack"])
ha = dec and codec.decode_hello_ack(*dec)
check("HELLO_ACK", bool(ha) and ha.mcu_boot_id == 0x1234 and ha.fw_ver == 0x100 and ha.proto_ver == 1
      and dec[1:5] == (1, 7, 1500, 0x27))

dec = codec.decode_frame(c["telemetry"])
tf = dec and codec.decode_telemetry(*dec)
check("TELEMETRY", bool(tf) and (tf.setpoint_mms, tf.speed_mms, tf.current_ma, tf.temp_c10, tf.pwm_pm,
                                 tf.flags, tf.seq) == (120, 118, 850, 342, 999, 0x37, 40))

dec = codec.decode_frame(c["telemetry_neg"])
tf = dec and codec.decode_telemetry(*dec)
check("TELEMETRY negative temp / wrap", bool(tf) and tf.temp_c10 == -125 and tf.seq == 0xFFFF
      and tf.tick_ms == 0xFFFFFFFF and tf.fault_reason == 3 and tf.sensor_status == 5)

dec = codec.decode_frame(c["mcu_event"])
ev = dec and codec.decode_mcu_event(*dec)
check("MCU_EVENT", bool(ev) and int(ev.event_kind) == 0x02 and ev.reason == 0x03 and ev.event_seq == 9
      and ev.observed == 3000 and ev.threshold == 2500)
dec = codec.decode_frame(c["mcu_event2"])
ev = dec and codec.decode_mcu_event(*dec)
check("MCU_EVENT negative observed", bool(ev) and ev.observed == -1 and ev.event_seq == 0x100)

dec = codec.decode_frame(c["cmd_ack"])
ca = dec and codec.decode_cmd_ack(*dec)
check("CMD_ACK", bool(ca) and ca.cmd_id == 0xBEEF and ca.result == 1 and ca.reason == 0x0E)

dec = codec.decode_frame(c["voice_event"])
ve = dec and codec.decode_voice_event(*dec)
check("VOICE_EVENT", bool(ve) and ve.kind == 1 and ve.confidence_pct == 93 and ve.event_seq == 0x201)
py_ve = codec.encode_voice_event(21, 22, 23, 0x67, 1, 93, 0x201, 1, 1)
check("VOICE_EVENT bytes identical", py_ve == c["voice_event"], f"{py_ve.hex()} vs {c['voice_event'].hex()}")

# Re-encode every frame with Python's generic encoder: must be byte-identical
for name, raw in c.items():
    if name.startswith("cobs"):
        continue
    t, seq, ack, tick, flags, payload = codec.decode_frame(raw)
    check(f"{name} byte-identical to codec.encode_frame", codec.encode_frame(t, seq, ack, tick, flags, payload) == raw)

# COBS vectors
v3 = bytes(((i % 255) + 1) for i in range(300))
for name, data in (("cobs1", b"\x00"), ("cobs2", b"\x11\x00\x22\x00\x00\x33"), ("cobs3", v3[:254]),
                   ("cobs4", v3), ("cobs5", b"")):
    check(f"{name} identical to transport.cobs_encode", cobs_encode(data) == c[name],
          f"{cobs_encode(data).hex()} vs {c[name].hex()}")

# ---- 2. MPU -> MCU: Python encodes, C parses ----------------------------------
frames = [
    (codec.encode_hello(1, 0, 10, 0xDEADBEEF), "type=1 seq=1 ack=0 tick=10 flags=0 rc=0", "proto=1 session=3735928559"),
    (codec.encode_heartbeat(2, 5, 20, 3), "type=2 seq=2 ack=5 tick=20", "mpu_state=3"),
    (codec.encode_event_ack(3, 6, 30, 0x1234), "type=3 seq=3", "event_seq=4660"),
    (codec.encode_run(4, 7, 40, 11, 500, 1, 300), "type=16 seq=4", "cmd_id=11 ttl=500 kind=1 sp=300"),
    (codec.encode_set_speed(5, 8, 50, 12, 400, 250), "type=17", "cmd_id=12 ttl=400 kind=0 sp=250"),
    (codec.encode_stop(6, 9, 60, 13, 500, 1), "type=18", "cmd_id=13 ttl=500 kind=1"),
    (codec.encode_halt(0xFFFF, 10, 0xFFFFFFFF, 14, 500), "type=19 seq=65535 ack=10 tick=4294967295", "cmd_id=14 ttl=500"),
    (codec.encode_clear_fault(8, 11, 80, 15, 500), "type=20", "cmd_id=15 ttl=500"),
]
stdin = "\n".join(f.hex() for f, _, _ in frames) + "\n"
res = subprocess.run([binary, "dec"], input=stdin, capture_output=True, text=True, check=True).stdout.splitlines()
for (frame, hdr, fields), line in zip(frames, res):
    check(f"C parses {hdr.split()[0]}", hdr in line and fields in line and "rc=0" in line, line)

# Corrupted frame must be rejected by C
bad = bytearray(codec.encode_heartbeat(1, 0, 0, 1))
bad[-1] ^= 0xFF
res = subprocess.run([binary, "dec"], input=bad.hex() + "\n", capture_output=True, text=True).stdout
check("C rejects bad CRC", "decode_err -4" in res, res)

print("\nALL CROSS-CHECKS PASSED" if fails == 0 else f"\n{fails} CROSS-CHECK(S) FAILED")
sys.exit(1 if fails else 0)
