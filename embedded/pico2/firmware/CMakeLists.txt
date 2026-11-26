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
  elseif(NOT CONFIG_EAA_KWS_TFLM)
    target_sources(app PRIVATE src/voice/kws_stub.c)
  endif()
endif()

# Wake word "Hey Edgie": MFCC + energy VAD + DS-CNN int8 on TFLite Micro / CMSIS-NN.
if(CONFIG_EAA_KWS_TFLM)
  target_sources(app PRIVATE
    src/voice/kws_frontend.c
    src/voice/kws_pipeline.c
    src/voice/kws_engine.cpp
    src/voice/model/kws_s.cc
    src/voice/model/kws_xs.cc)
  if(CONFIG_EAA_KWS_CMSIS_DSP)
    target_compile_definitions(app PRIVATE KWS_FE_CMSIS_DSP)
  endif()
  if(CONFIG_EAA_KWS_CODE_IN_RAM)
    # RP2350 executes from QSPI flash through a 16 KB XIP cache; the hot DSP / NN
    # kernels and the front end are copied to SRAM at boot instead.
    # Only the files on the hot path (relocating whole libraries drags every
    # object and its tables into RAM). Code goes to SRAM, constant tables stay in flash.
    set(NN ${ZEPHYR_CMSIS_NN_MODULE_DIR}/Source)
    set(DSP ${ZEPHYR_CMSIS_DSP_MODULE_DIR}/Source)
    file(GLOB KWS_HOT_NN ${NN}/ConvolutionFunctions/*.c ${NN}/NNSupportFunctions/*.c
         ${NN}/FullyConnectedFunctions/*.c)
    zephyr_code_relocate(FILES ${CMAKE_CURRENT_SOURCE_DIR}/src/voice/kws_frontend.c
                         ${DSP}/TransformFunctions/arm_rfft_fast_f32.c
                         ${DSP}/TransformFunctions/arm_cfft_f32.c
                         ${DSP}/TransformFunctions/arm_cfft_radix8_f32.c
                         ${DSP}/TransformFunctions/arm_bitreversal2.c
                         ${DSP}/ComplexMathFunctions/arm_cmplx_mag_squared_f32.c
                         ${KWS_HOT_NN}
                         LOCATION RAM_TEXT)
  endif()
  if(CONFIG_EAA_KWS_BENCH)
    target_sources(app PRIVATE src/voice/kws_bench.c src/voice/model/kws_bench_clips.cc
        src/voice/model/kws_ei.cc src/voice/model/kws_tiny.cc
        src/voice/model/kws_cnn.cc src/voice/model/kws_tcres8.cc)
  endif()
endif()
