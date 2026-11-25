# Build the EAA MCU firmware for the Raspberry Pi Pico 2 (Windows PowerShell).
#   .\build.ps1            -> full build (INA226 + MLX90614 required)
#   .\build.ps1 bench      -> bench build (no current/temperature sensors fitted yet)
#   .\build.ps1 pctest     -> PC hardware-in-the-loop build (B-03 bridge on USB, see boards/pc_test.overlay)
#   .\build.ps1 test       -> host unit tests (protocol + safety state machine)
# Toolchain: run ..\tools\setup_zephyr.sh once (Git Bash) -> %USERPROFILE%\zephyrproject
#   .\build.ps1 unoq       -> Arduino UNO Q MCU (STM32U585) build, compile-only check for now
param([ValidateSet("full", "bench", "pctest", "unoq", "test")][string]$Variant = "full")

$ErrorActionPreference = "Continue"  # west/cmake write progress to stderr; check $LASTEXITCODE
$ZP = "$env:USERPROFILE\zephyrproject"
$env:ZEPHYR_BASE = "$ZP\zephyr"
$env:ZEPHYR_SDK_INSTALL_DIR = "$ZP\sdk\zephyr-sdk-0.17.2"
$env:PATH = "$ZP\.venv\Scripts;$env:PATH"   # west, cmake, ninja live in the venv
$py = "$ZP\.venv\Scripts\python.exe"

if ($Variant -eq "test") {
    $out = "$ZP\hosttest"; New-Item -ItemType Directory -Force $out | Out-Null
    Push-Location "$PSScriptRoot\tests\host"
    & $py -m ziglang cc -std=c99 -Wall -I..\..\src -o "$out\b03_host_test.exe" b03_host_test.c ..\..\src\proto\crc16.c ..\..\src\proto\cobs.c ..\..\src\proto\b03.c
    & "$out\b03_host_test.exe"
    & $py -m ziglang cc -std=c11 -Wall -Ishim -I..\..\src -o "$out\safety_host_test.exe" safety_host_test.c ..\..\src\proto\b03.c ..\..\src\proto\crc16.c
    & "$out\safety_host_test.exe"
    Write-Host "Cross-check with the orchestrator codec: python cross_check.py $out\b03_host_test.exe <orchestrator>\src"
    Pop-Location
    exit $LASTEXITCODE
}

# Build directory kept short: Windows paths over 260 characters break the devicetree step.
$bd = "$ZP\b-$Variant"
$extra = @()
if ($Variant -eq "bench") { $extra = @("--", "-DCONFIG_EAA_INA226=n", "-DCONFIG_EAA_MLX90614=n") }
if ($Variant -eq "pctest") {
    $extra = @("--", "-DCONFIG_EAA_INA226=n", "-DCONFIG_EAA_MLX90614=n",
               "-DEXTRA_DTC_OVERLAY_FILE=boards/pc_test.overlay")
}
if ($Variant -eq "unoq") {
    # Real target: Arduino UNO Q MCU (STM32U585). Board files in boards/arduino/uno_q (from Zephyr main).
    & "$ZP\.venv\Scripts\west.exe" build -p auto -b arduino_uno_q $PSScriptRoot -d $bd -- "-DBOARD_ROOT=$PSScriptRoot"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Copy-Item "$bd\zephyr\zephyr.bin" "$PSScriptRoot\..\release\eaa_mcu_unoq.bin" -Force
    Write-Host "UNO Q image: release\eaa_mcu_unoq.bin (flash at 0x08010000, slot0; ELF in $bd\zephyr) - not tested on a board"
    exit 0
}
& "$ZP\.venv\Scripts\west.exe" build -p auto -b rpi_pico2/rp2350a/m33 $PSScriptRoot -d $bd @extra
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$dst = "$PSScriptRoot\..\release\eaa_mcu_pico2_$Variant.uf2"
Copy-Item "$bd\zephyr\zephyr.uf2" $dst -Force
Write-Host "UF2: $dst  (hold BOOTSEL, plug USB, drag the file onto the RP2350 drive)"
