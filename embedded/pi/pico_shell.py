"""Small helper for the Pico 2 shell over USB CDC (console = first CDC interface).

  python pico_shell.py COM7 "kws bench"           # run commands, print output
  python pico_shell.py COM7 --flash fw.uf2        # 'eaa bootsel' then copy the UF2
"""
import argparse
import shutil
import string
import sys
import time
from pathlib import Path

import serial


def run(port, cmds, wait=2.0, idle=1.5):
    s = serial.Serial(port, 115200, timeout=0.2, write_timeout=1.0)
    s.reset_input_buffer()
    s.write(b"\r\n")
    time.sleep(0.3)
    s.read(65536)
    out = []
    for c in cmds:
        s.write(c.encode() + b"\r\n")
        t_end, last = time.time() + wait, time.time()
        buf = b""
        while True:
            try:
                d = s.read(4096)
            except serial.SerialException:
                buf += b" [port lost: device reset or USB re-enumeration]"
                out.append(buf.decode(errors="replace"))
                return out
            if d:
                buf += d
                last = time.time()
            elif time.time() - last > idle and time.time() > t_end:
                break
            if time.time() - last > 120:
                break
        out.append(buf.decode(errors="replace"))
    s.close()
    return out


def find_drive():
    for letter in string.ascii_uppercase[3:]:
        p = Path(f"{letter}:/INFO_UF2.TXT")
        try:
            if p.exists() and "RP2350" in p.read_text(errors="ignore"):
                return Path(f"{letter}:/")
        except OSError:
            pass
    return None


def flash(port, uf2):
    try:
        s = serial.Serial(port, 115200, timeout=0.2, write_timeout=1.0)
        s.write(b"\r\neaa bootsel\r\n")
        s.close()
    except (serial.SerialException, OSError) as e:
        print("bootsel command:", e)
    for _ in range(240):  # Windows can take >30 s to mount the drive
        d = find_drive()
        if d:
            shutil.copy(uf2, d / Path(uf2).name)
            print("copied to", d)
            break
        time.sleep(0.5)
    else:
        sys.exit("RP2350 drive not found")
    for _ in range(60):  # wait for the console port to come back
        time.sleep(0.5)
        try:
            serial.Serial(port).close()
            time.sleep(1.5)
            return
        except serial.SerialException:
            pass
    sys.exit("console port did not come back")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("cmds", nargs="*")
    ap.add_argument("--flash")
    ap.add_argument("--wait", type=float, default=2.0)
    a = ap.parse_args()
    if a.flash:
        flash(a.port, a.flash)
    for c, o in zip(a.cmds, run(a.port, a.cmds, a.wait) if a.cmds else []):
        print(f"### {c}\n{o}")
