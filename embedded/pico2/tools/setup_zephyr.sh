#!/usr/bin/env bash
set -e
cd ~/zephyrproject
# One-time toolchain install (Git Bash on Windows): Zephyr v4.2.0 (only the modules this
# firmware needs) + Zephyr SDK 0.17.2 arm-zephyr-eabi into ~/zephyrproject, with a Python venv.
# Before the first run:  python -m venv ~/zephyrproject/.venv && ~/zephyrproject/.venv/Scripts/pip install west cmake ninja py7zr ziglang
PY=./.venv/Scripts/python; WEST=./.venv/Scripts/west
./.venv/Scripts/pip install -q "cmake<4" 
echo "== west init"; [ -d .west ] || $WEST init -m https://github.com/zephyrproject-rtos/zephyr --mr v4.2.0 .
$WEST config manifest.project-filter -- "-.*,+hal_rpi_pico,+cmsis,+cmsis_6,+picolibc"
echo "== west update"; $WEST update --narrow -o=--depth=1
echo "== pip reqs"; ./.venv/Scripts/pip install -q -r zephyr/scripts/requirements-base.txt
echo "== sdk"; mkdir -p sdk && cd sdk
V=0.17.2; B=https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v$V
[ -f min.7z ] || curl -sSL -o min.7z $B/zephyr-sdk-${V}_windows-x86_64_minimal.7z
[ -f arm.7z ] || curl -sSL -o arm.7z $B/toolchain_windows-x86_64_arm-zephyr-eabi.7z
ls -la
../.venv/Scripts/python -c "import py7zr;py7zr.SevenZipFile('min.7z').extractall('.')"
cd zephyr-sdk-$V && ../../.venv/Scripts/python -c "import py7zr;py7zr.SevenZipFile('../arm.7z').extractall('.')"
ls; echo "== DONE"
