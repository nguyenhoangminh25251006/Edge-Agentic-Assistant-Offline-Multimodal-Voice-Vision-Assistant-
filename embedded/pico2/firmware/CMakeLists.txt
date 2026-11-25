cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(eaa_mcu LANGUAGES C CXX)

target_include_directories(app PRIVATE src)
target_sources(app PRIVATE
  src/main.c
  src/proto/crc16.c
  src/proto/cobs.c
  src/proto/b03.c
  src/bridge.c
  src/safety.c
  src/control.c
  src/motor.c
  src/encoder.c
  src/sensors.c
)

if(CONFIG_EAA_VOICE)
  target_sources(app PRIVATE src/voice/voice.c src/voice/audio_out.c)
  if(CONFIG_SOC_SERIES_RP2350)
    target_sources(app PRIVATE src/voice/i2s_in_rp2.c)
  else()
    message(FATAL_ERROR "No microphone driver for this board: implement voice/audio_in.h "
                        "with the Zephyr I2S/DMIC API (e.g. STM32U585 SAI)")
  endif()

  if(CONFIG_EAA_KWS_EDGE_IMPULSE)
    set(EI_DIR ${CMAKE_CURRENT_SOURCE_DIR}/ei-model)
    if(NOT EXISTS ${EI_DIR}/edge-impulse-sdk)
      message(FATAL_ERROR "CONFIG_EAA_KWS_EDGE_IMPULSE=y but ${EI_DIR} has no Edge Impulse export")
    endif()
    # Edge Impulse SDK ships a Zephyr CMake helper that adds the SDK sources to 'app'.
    add_subdirectory(${EI_DIR}/edge-impulse-sdk/cmake/zephyr ei_sdk)
    target_include_directories(app PRIVATE ${EI_DIR})
    file(GLOB EI_MODEL_SRC ${EI_DIR}/tflite-model/*.cpp)
    target_sources(app PRIVATE ${EI_MODEL_SRC} src/voice/kws_ei.cpp)
    target_compile_definitions(app PRIVATE
      EI_CLASSIFIER_SLICES_PER_MODEL_WINDOW=4
      EI_CLASSIFIER_TFLITE_ENABLE_CMSIS_NN=1)
  else()
    target_sources(app PRIVATE src/voice/kws_stub.c)
  endif()
endif()
