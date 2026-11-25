"""Bench tool for milestone M2 (CH-239): talk B-03 to the Pico 2 over the Pi UART.

    python3 link_test.py --port /dev/serial0            # interactive
    python3 link_test.py --port /dev/serial0 --soak 600 # 10-minute link soak test

It acts like the orchestrator's McuBridgeWorker: HELLO until HELLO_ACK, HEARTBEAT every
100 ms, EVENT_ACK for every MCU_EVENT / VOICE_EVENT. Commands (type at the prompt):
    run <mm/s>    speed <mm/s>    stop    pause    halt    clear
    freeze <ms>   stop sending heartbeats (simulates a frozen MPU -> SAFE_STOP in 300 ms)
    quiet / loud  hide / show the telemetry lines        stats    quit
"""
from __future__ import annotations

import argparse
import random
import struct
import threading
import time

import serial  # pip install pyserial

import b03


class Link:
    def __init__(self, port: str, baud: int = 115200, show_telem: bool = True) -> None:
        self.ser = serial.serial_for_url(port, baudrate=baud, timeout=0.05, write_timeout=1.0)  # also socket://host:port for tests
        self.lock = threading.Lock()
        self.seq = 0
        self.last_rx_seq = 0
        self.cmd_id = random.randint(1, 1000)
        self.t0 = time.monotonic()
        self.synced = False
        self.freeze_until = 0.0
        self.show_telem = show_telem
        self.stats = {"rx": 0, "bad": 0, "telem": 0, "events": 0, "dup_events": 0, "acks": 0}
        self.seen_events: set[int] = set()
        self.latest = ""
        self.telem: dict = {}
        self.running = True
        self.session = random.getrandbits(32)
        # for automated tests (hil_test.py)
        self.auto_ack = True
        self.verbose = True
        self.acks: dict[int, tuple[int, int, int]] = {}       # cmd_id -> (result, reason, state)
        self.event_log: list[tuple[float, int, int, int, int]] = []  # (t, msg_type, kind, reason, event_seq), every arrival
        self.last_hb_tx = 0.0

    def tick(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def send(self, t: int, payload: bytes) -> None:
        with self.lock:
            frame = b03.encode(t, self.seq, self.last_rx_seq, self.tick(), 0, payload)
            self.seq = (self.seq + 1) & 0xFFFF
            self.ser.write(b03.cobs_encode(frame) + b"\x00")

    def next_id(self) -> int:
        self.cmd_id = (self.cmd_id + 1) & 0xFFFF
        return self.cmd_id

    def rx_loop(self) -> None:
        buf = bytearray()
        while self.running:
            chunk = self.ser.read(256)
            for byte in chunk:
                if byte != 0:
                    buf.append(byte)
                    continue
                raw = b03.cobs_decode(bytes(buf)) if buf else None
                buf.clear()
                d = b03.decode(raw) if raw else None
                if d is None:
                    self.stats["bad"] += 1
                    continue
                t, seq, ack, tick, flags, p = d
                self.last_rx_seq = seq
                self.stats["rx"] += 1
                text = b03.describe(t, flags, p)
                if t == b03.HELLO_ACK:
                    if not self.synced and self.verbose:
                        print(f"\n<< {text}")
                    self.synced = True
                elif t == b03.TELEMETRY:
                    self.stats["telem"] += 1
                    self.latest = text
                    st, fr, sp, spd, cur, temp, ss, pwm = struct.unpack_from("<BBHHHhBH", p)
                    self.telem = {"state": st, "fault": fr, "setpoint_mms": sp, "speed_mms": spd,
                                  "current_ma": cur, "temp_c10": temp, "flags": flags}
                    if self.show_telem and self.stats["telem"] % 10 == 0:
                        print(f"<< {text}")
                elif t in (b03.MCU_EVENT, b03.VOICE_EVENT):
                    es = struct.unpack_from("<H", p, 2)[0]
                    self.event_log.append((time.monotonic(), t, p[0], p[1], es))
                    if self.auto_ack:
                        self.send(b03.EVENT_ACK, b03.p_event_ack(es))
                    if es in self.seen_events:
                        self.stats["dup_events"] += 1  # retransmission we already handled
                    else:
                        self.seen_events.add(es)
                        self.stats["events"] += 1
                        if self.verbose:
                            print(f"\n<< {text}")
                elif t == b03.CMD_ACK:
                    self.stats["acks"] += 1
                    cid, res, rs, st = struct.unpack_from("<HBBB", p)
                    self.acks[cid] = (res, rs, st)
                    if self.verbose:
                        print(f"<< {text}")

    def tx_loop(self) -> None:
        last_hello = 0.0
        while self.running:
            now = time.monotonic()
            if now >= self.freeze_until:
                if not self.synced and now - last_hello > 0.5:
                    self.send(b03.HELLO, b03.p_hello(self.session))
                    last_hello = now
                self.send(b03.HEARTBEAT, b03.p_heartbeat(1))
                self.last_hb_tx = time.monotonic()
            time.sleep(0.1)

    def start(self) -> None:
        threading.Thread(target=self.rx_loop, daemon=True).start()
        threading.Thread(target=self.tx_loop, daemon=True).start()


def interactive(link: Link) -> None:
    print(__doc__)
    while True:
        try:
            parts = input("> ").split()
        except (EOFError, KeyboardInterrupt):
            break
        if not parts:
            continue
        c, arg = parts[0], (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None)
        if c == "run":
            link.send(b03.RUN, b03.p_run(link.next_id(), 500, 0, arg or 150))
        elif c == "speed":
            link.send(b03.SET_SPEED, b03.p_set_speed(link.next_id(), 500, arg or 150))
        elif c == "stop":
            link.send(b03.STOP, b03.p_stop(link.next_id(), 500, 0))
        elif c == "pause":
            link.send(b03.STOP, b03.p_stop(link.next_id(), 500, 1))
        elif c == "halt":
            link.send(b03.HALT, b03.p_cmd(link.next_id(), 500))
        elif c == "clear":
            link.send(b03.CLEAR_FAULT, b03.p_cmd(link.next_id(), 500))
        elif c == "freeze":
            link.freeze_until = time.monotonic() + (arg or 1000) / 1000
            print(f"heartbeats paused for {arg or 1000} ms")
        elif c == "quiet":
            link.show_telem = False
        elif c == "loud":
            link.show_telem = True
        elif c == "stats":
            print(link.stats, "\nlast:", link.latest)
        elif c in ("quit", "exit"):
            break
        else:
            print("unknown command")


def soak(link: Link, seconds: int) -> None:
    t_end = time.monotonic() + seconds
    while not link.synced and time.monotonic() < t_end:
        time.sleep(0.1)
    print("synced" if link.synced else "NO HELLO_ACK")
    start = dict(link.stats)
    t_start = time.monotonic()
    while time.monotonic() < t_end:
        time.sleep(10)
        el = time.monotonic() - t_start
        rate = (link.stats["telem"] - start["telem"]) / el
        print(f"{el:6.0f} s  telemetry {rate:5.1f} Hz  bad frames {link.stats['bad'] - start['bad']}  | {link.latest}")
    el = time.monotonic() - t_start
    rate = (link.stats["telem"] - start["telem"]) / el
    bad = link.stats["bad"] - start["bad"]
    ok = link.synced and bad == 0 and rate > 18
    print(f"\nSOAK {'PASS' if ok else 'FAIL'}: {el:.0f} s, telemetry {rate:.1f} Hz (target 20), bad frames {bad}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--soak", type=int, default=0, help="run an N-second soak test instead of the prompt")
    a = ap.parse_args()
    lk = Link(a.port, a.baud, show_telem=not a.soak)
    lk.start()
    soak(lk, a.soak) if a.soak else interactive(lk)
    lk.running = False
