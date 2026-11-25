# Arduino UNO Q board (MCU side, STM32U585) for Zephyr v4.2

Copied from upstream Zephyr `main` (boards/arduino/uno_q, Apache-2.0, (c) Arduino SA),
because Zephyr v4.2 (used for the Pico 2 build) does not have this board yet.
Changes for v4.2: LED-matrix node removed (no driver in v4.2), flash partitions declared
with `fixed-partitions` instead of `zephyr,mapped-partition`.
Delete this folder once the project moves to a Zephyr release that ships `arduino_uno_q`.
