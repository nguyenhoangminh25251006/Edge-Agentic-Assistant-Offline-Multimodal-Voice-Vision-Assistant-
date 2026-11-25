"""Automated hardware-in-the-loop test of the EAA MCU firmware (B-03 link + safety FSM).

On a PC with only the Pico 2 plugged in (firmware built with `build.ps1 pctest`):
    python hil_test.py --link COM8 --console COM7 --audio COM6
On the Pi 3B with the real wiring (bench/full firmware; GP11 jumpered to GND):
    python3 hil_test.py --link /dev/serial0 --console /dev/ttyACM0 --audio /dev/ttyACM1

--link    B-03 control link (the port the orchestrator would use)
--console Zephyr shell of the Pico (used for voice simulation and jitter statistics)
--audio   command-audio channel (optional)
Exit code 0 = every check passed. Results are printed as a table for the report.
"""
from __future__ import annotations

import argparse
import re
import statistics
import struct
import sys
import threading
import time

import serial

import b03
from link_test import Link

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    return ok


def wait(cond, timeout: float, step: float = 0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class Console:
    def __init__(self, port: str) -> None:
        self.s = serial.Serial(port, 115200, timeout=0.2)
        self.s.dtr = True
        time.sleep(0.2)
        self.s.reset_input_buffer()

    def cmd(self, line: str, wait_s: float = 0.6) -> str:
        self.s.reset_input_buffer()
        self.s.write(line.encode() + b"\r\n")
        time.sleep(wait_s)
        return re.sub(r"\x1b\[[0-9;]*m", "", self.s.read(8000).decode(errors="replace"))


def send_cmd(lk: Link, t: int, payload_fn, *args, cmd_id: int | None = None, timeout: float = 1.0):
    cid = cmd_id if cmd_id is not None else lk.next_id()
    lk.acks.pop(cid, None)
    lk.send(t, payload_fn(cid, *args))
    wait(lambda: cid in lk.acks, timeout)
    return lk.acks.get(cid)


def state(lk: Link) -> int:
    return lk.telem.get("state", -1)


def events_since(lk: Link, t0: float, kind: int | None = None, msg_type: int = b03.MCU_EVENT):
    return [e for e in lk.event_log if e[0] >= t0 and e[1] == msg_type and (kind is None or e[2] == kind)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--link", required=True)
    ap.add_argument("--console", required=True)
    ap.add_argument("--audio", default="")
    ap.add_argument("--jitter-seconds", type=int, default=20)
    a = ap.parse_args()

    con = Console(a.console)
    lk = Link(a.link, show_telem=False)
    lk.verbose = False
    lk.start()
    ACC, REJ, DUP = 0, 1, 2

    # 1. Handshake and telemetry -------------------------------------------------
    t0 = time.monotonic()
    check("HELLO -> HELLO_ACK", wait(lambda: lk.synced, 3.0), f"{(time.monotonic() - t0) * 1000:.0f} ms")
    wait(lambda: state(lk) != 3 or lk.telem.get("fault") != 0x05, 1.0)
    if state(lk) == 3:  # a latched fault from before (e.g. earlier heartbeat loss): clear it
        send_cmd(lk, b03.CLEAR_FAULT, b03.p_cmd, 500)
    n0, b0, t0 = lk.stats["telem"], lk.stats["bad"], time.monotonic()
    time.sleep(5)
    rate = (lk.stats["telem"] - n0) / (time.monotonic() - t0)
    check("TELEMETRY rate ~20 Hz", 18.5 <= rate <= 21.0, f"{rate:.2f} Hz")
    check("no bad frames from MCU", lk.stats["bad"] - b0 == 0, f"{lk.stats['bad'] - b0} bad")
    fl = lk.telem.get("flags", 0)
    check("flags: E-stop closed, beam clear, HB valid", (fl & 0b111) == 0b111, f"flags=0x{fl:02x}")
    check("state STOPPED after sync", state(lk) == 1, b03.STATES.get(state(lk), "?"))

    # 2. Command validation -------------------------------------------------------
    r = send_cmd(lk, b03.SET_SPEED, b03.p_set_speed, 500, 600)
    check("SET_SPEED 600 mm/s rejected LIMIT_EXCEEDED", r is not None and r[:2] == (REJ, 0x0C), str(r))
    r = send_cmd(lk, b03.RUN, b03.p_run, 500, 0, 20)
    check("RUN 20 mm/s rejected BAD_PARAM", r is not None and r[:2] == (REJ, 0x08), str(r))
    r = send_cmd(lk, b03.RUN, b03.p_run, 0, 0, 150)
    check("RUN with ttl 0 rejected EXPIRED", r is not None and r[:2] == (REJ, 0x09), str(r))
    lk.send(0x7E, b"")  # unknown type: the MCU ignores it (no cmd_id to answer)

    # 3. RUN, ramp, duplicate -----------------------------------------------------
    cid = lk.next_id()
    t_run = time.monotonic()
    r = send_cmd(lk, b03.RUN, b03.p_run, 500, 0, 150, cmd_id=cid)
    ack_ms = (time.monotonic() - t_run) * 1000
    check("RUN 150 mm/s accepted", r is not None and r[0] == ACC and r[2] == 2, f"{r}, ack in {ack_ms:.0f} ms")
    r2 = send_cmd(lk, b03.RUN, b03.p_run, 500, 0, 150, cmd_id=cid)
    check("same cmd_id again -> DUPLICATE", r2 is not None and r2[0] == DUP, str(r2))
    ramp_ok = wait(lambda: lk.telem.get("speed_mms") == 150, 2.0)
    check("ramp reaches 150 mm/s", ramp_ok, f"after {(time.monotonic() - t_run):.2f} s (250 mm/s^2 -> ~0.6 s)")
    time.sleep(0.2)
    st_line = con.cmd("eaa status")
    m = re.search(r"pwm (\d+)/1000", st_line)
    check("open-loop PWM = 100 + 1.8*150 = 370", bool(m) and int(m.group(1)) == 370, m.group(0) if m else st_line[-200:])
    check("MOTOR_ENABLED flag set", bool(lk.telem.get("flags", 0) & 0x10), f"flags=0x{lk.telem.get('flags', 0):02x}")

    # 4. Heartbeat watchdog (MPU frozen) ----------------------------------------
    t_ev = time.monotonic()
    lk.freeze_until = time.monotonic() + 1.5
    last_hb = lk.last_hb_tx
    ok = wait(lambda: events_since(lk, t_ev, 0x04), 1.5)
    if ok:
        ev_t = events_since(lk, t_ev, 0x04)[0][0]
        dt = (ev_t - last_hb) * 1000
        check("heartbeat lost -> HEARTBEAT_TIMEOUT event", 300 <= dt <= 400, f"{dt:.0f} ms after last heartbeat (limit 300 ms)")
    else:
        check("heartbeat lost -> HEARTBEAT_TIMEOUT event", False, "no event")
    wait(lambda: state(lk) == 3, 1.0)
    check("state SAFE_STOP, speed 0, fault latched", state(lk) == 3 and lk.telem.get("speed_mms") == 0
          and bool(lk.telem.get("flags", 0) & 0x08), lk.latest)
    time.sleep(1.6)  # heartbeats resume
    r = send_cmd(lk, b03.RUN, b03.p_run, 500, 0, 150)
    check("RUN refused while latched (FAULT_LATCHED)", r is not None and r[:2] == (REJ, 0x0E), str(r))
    t_ev = time.monotonic()
    r = send_cmd(lk, b03.CLEAR_FAULT, b03.p_cmd, 500)
    check("CLEAR_FAULT accepted once heartbeat is back", r is not None and r[0] == ACC and r[2] == 1, str(r))
    check("FAULT_CLEARED event", wait(lambda: events_since(lk, t_ev, 0x09), 1.0))

    # 5. HALT + event retransmission until EVENT_ACK -----------------------------
    send_cmd(lk, b03.RUN, b03.p_run, 500, 0, 120)
    lk.auto_ack = False
    t_ev = time.monotonic()
    r = send_cmd(lk, b03.HALT, b03.p_cmd, 500)
    check("HALT accepted -> SAFE_STOP", r is not None and r[0] == ACC and r[2] == 3, str(r))
    time.sleep(1.05)
    arrivals = events_since(lk, t_ev, 0x01)
    seqs = {e[4] for e in arrivals}
    gaps = [(arrivals[i + 1][0] - arrivals[i][0]) * 1000 for i in range(len(arrivals) - 1)]
    check("un-ACKed event retransmitted every ~100 ms, same event_seq", len(arrivals) >= 8 and len(seqs) == 1
          and (not gaps or 80 <= statistics.median(gaps) <= 130),
          f"{len(arrivals)} copies in 1 s, median gap {statistics.median(gaps) if gaps else 0:.0f} ms, event_seq {seqs}")
    lk.auto_ack = True
    time.sleep(0.4)
    n_before = len(events_since(lk, t_ev, 0x01))
    time.sleep(0.5)
    check("retransmission stops after EVENT_ACK", len(events_since(lk, t_ev, 0x01)) == n_before)
    st_line = con.cmd("eaa status")
    check("status shows fault MPU_HALT_REQUEST (0x0d)", "fault 0x0d" in st_line, st_line.splitlines()[1] if st_line else "")
    send_cmd(lk, b03.CLEAR_FAULT, b03.p_cmd, 500)

    # 6. Corrupted input does not break the link ---------------------------------
    st0 = con.cmd("eaa status")
    m0 = re.search(r"bad_crc (\d+) bad_cobs (\d+)", st0)
    good = b03.encode(b03.HEARTBEAT, 999, 0, 0, 0, b03.p_heartbeat(1))
    bad = bytearray(good)
    bad[-1] ^= 0x5A
    lk.ser.write(b03.cobs_encode(bytes(bad)) + b"\x00" + b"\x07\x01\x02\x00" + bytes(range(1, 120)) + b"\x00")
    time.sleep(0.5)
    st1 = con.cmd("eaa status")
    m1 = re.search(r"bad_crc (\d+) bad_cobs (\d+)", st1)
    check("bad CRC and broken COBS frames counted and dropped",
          bool(m0 and m1) and int(m1.group(1)) > int(m0.group(1)) and int(m1.group(2)) > int(m0.group(2)),
          f"{m0.group(0) if m0 else '?'} -> {m1.group(0) if m1 else '?'}")
    r = send_cmd(lk, b03.STOP, b03.p_stop, 500, 0)
    check("link still works after garbage", r is not None and r[0] == ACC, str(r))

    # 7. Voice front end ----------------------------------------------------------
    rx = None
    if a.audio:
        import audio_rx
        utt: list = []
        rx = audio_rx.AudioReceiver(a.audio, lambda pcm, rate: utt.append((len(pcm) // 2, rate)))
        stop_rx = threading.Event()
        threading.Thread(target=lambda: [rx.poll() for _ in iter(stop_rx.is_set, True)], daemon=True).start()
    t_ev = time.monotonic()
    con.cmd("eaa wake", 0.3)
    ok = wait(lambda: events_since(lk, t_ev, 0, b03.VOICE_EVENT), 1.0)
    dt = (events_since(lk, t_ev, 0, b03.VOICE_EVENT)[0][0] - t_ev) * 1000 if ok else 0
    check("wake -> VOICE_EVENT WAKE", ok, f"{dt:.0f} ms")
    check("VOICE_ACTIVE flag while listening", wait(lambda: bool(lk.telem.get("flags", 0) & 0x40), 0.5))
    if a.audio:
        wait(lambda: utt, 7.0)
        check("5 s of command audio received", bool(utt) and abs(utt[0][0] - 80000) < 800 and utt[0][1] == 16000,
              f"{utt[0] if utt else 'none'} samples/rate, {rx.bad} bad audio frames")
        stop_rx.set()
    else:
        time.sleep(5.2)
    check("VOICE_ACTIVE cleared after the window", wait(lambda: not lk.telem.get("flags", 0) & 0x40, 1.0))

    send_cmd(lk, b03.RUN, b03.p_run, 500, 0, 200)
    wait(lambda: lk.telem.get("speed_mms") == 200, 2.0)
    t_ev = time.monotonic()
    con.cmd("eaa vstop", 0.2)
    ok = wait(lambda: events_since(lk, t_ev, 1, b03.VOICE_EVENT), 1.0)
    check("spoken stop -> VOICE_EVENT STOP", ok)
    check("spoken stop -> MCU stops the belt by itself (controlled ramp)",
          wait(lambda: state(lk) == 1 and lk.telem.get("speed_mms") == 0, 2.0),
          f"stopped in {(time.monotonic() - t_ev):.2f} s")

    # 8. Control loop jitter under load (link + telemetry + mic ISR + running belt) --
    send_cmd(lk, b03.RUN, b03.p_run, 500, 0, 300)
    con.cmd("eaa jreset", 0.3)
    t_end = time.monotonic() + a.jitter_seconds
    while time.monotonic() < t_end:  # keep the shell and link busy too
        send_cmd(lk, b03.SET_SPEED, b03.p_set_speed, 500, 150 if int(time.monotonic()) % 2 else 300)
        time.sleep(0.25)
    st_line = con.cmd("eaa status")
    m = re.search(r"control loops (\d+), jitter max (\d+) us, exec max (\d+) us", st_line)
    if m:
        check(f"control loop jitter <= 1000 us over {a.jitter_seconds} s (NFR-01)", int(m.group(2)) <= 1000,
              f"jitter max {m.group(2)} us, exec max {m.group(3)} us, loops {m.group(1)}")
    else:
        check("jitter statistics readable", False, st_line[-200:])
    send_cmd(lk, b03.STOP, b03.p_stop, 500, 0)

    lk.running = False
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
