/* TensorFlow Lite Micro runner for the wake-word DS-CNN (CMSIS-NN int8 kernels).
 * Portable: identical on the Pico 2 (RP2350) and the UNO Q MCU (STM32U585). */
#include <new>
#include <zephyr/kernel.h>
#include <string.h>
#include <tensorflow/lite/micro/micro_interpreter.h>
#include <tensorflow/lite/micro/micro_mutable_op_resolver.h>
#include <tensorflow/lite/schema/schema_generated.h>
#include "kws_engine.h"

namespace {
constexpr size_t kArenaSize = CONFIG_EAA_KWS_ARENA_KB * 1024;
alignas(16) uint8_t arena[kArenaSize];
alignas(tflite::MicroInterpreter) uint8_t interp_mem[sizeof(tflite::MicroInterpreter)];
tflite::MicroMutableOpResolver<8> resolver;
tflite::MicroInterpreter *interp;
const kws_model_info *cur;
bool ops_added;
K_MUTEX_DEFINE(lock); /* live pipeline (voice thread) vs. benchmark (shell) */
} // namespace

static int init_locked(const struct kws_model_info *m)
{
	if (!ops_added) {
		resolver.AddConv2D();
		resolver.AddDepthwiseConv2D();
		resolver.AddAveragePool2D();
		resolver.AddMaxPool2D(); /* comparison models (Edge Impulse 1-D conv) */
		resolver.AddAdd();       /* comparison models (TC-ResNet residuals) */
		resolver.AddReshape();
		resolver.AddFullyConnected();
		resolver.AddSoftmax();
		ops_added = true;
	}
	if (interp) {
		interp->~MicroInterpreter();
		interp = nullptr;
	}
	const tflite::Model *model = tflite::GetModel(m->data);

	if (model->version() != TFLITE_SCHEMA_VERSION) {
		return -1;
	}
	interp = new (interp_mem) tflite::MicroInterpreter(model, resolver, arena, kArenaSize);
	if (interp->AllocateTensors() != kTfLiteOk) {
		interp->~MicroInterpreter();
		interp = nullptr;
		return -2;
	}
	TfLiteTensor *in = interp->input(0);

	if (in->type != kTfLiteInt8 || in->bytes != KWS_INPUT_BYTES) {
		interp->~MicroInterpreter();
		interp = nullptr;
		return -3;
	}
	cur = m;
	return 0;
}

extern "C" int kws_engine_init(const struct kws_model_info *m)
{
	k_mutex_lock(&lock, K_FOREVER);
	cur = nullptr;
	int rc = 0;

	if (m == nullptr) { /* release only */
		if (interp) {
			interp->~MicroInterpreter();
			interp = nullptr;
		}
	} else {
		rc = init_locked(m);
	}

	k_mutex_unlock(&lock);
	return rc;
}

extern "C" int kws_engine_infer(const int8_t *features, int8_t out[3])
{
	int rc = 0;

	k_mutex_lock(&lock, K_FOREVER);
	if (!interp || !cur) {
		rc = -1;
	} else {
		memcpy(interp->input(0)->data.int8, features, KWS_INPUT_BYTES);
		if (interp->Invoke() != kTfLiteOk) {
			rc = -2;
		} else {
			memcpy(out, interp->output(0)->data.int8, 3);
		}
	}
	k_mutex_unlock(&lock);
	return rc;
}

extern "C" size_t kws_engine_arena_used(void)
{
	return interp ? interp->arena_used_bytes() : 0;
}

extern "C" const struct kws_model_info *kws_engine_model(void)
{
	return cur;
}
