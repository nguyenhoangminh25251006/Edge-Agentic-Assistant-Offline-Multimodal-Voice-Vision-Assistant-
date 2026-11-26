"""Train the "Hey Edgie" wake-word model (DS-CNN on the MCU's MFCC) and export it.

  python train.py --arch s     # DS-CNN-S  (64 channels, 4 DS blocks) - accuracy
  python train.py --arch xs    # DS-CNN-XS (32 channels, 3 DS blocks) - speed
  Comparison set (same data, same MFCC, same int8 flow), see WAKEWORD_REPORT.md:
  python train.py --arch ei      # Edge Impulse default KWS: 2x 1-D conv (8, 16) + pooling
  python train.py --arch tiny    # TFLM micro_speech "tiny_conv": 1 conv 8@10x8/2 + FC
  python train.py --arch cnn     # Hello Edge CNN-S style: 2 conv (28, 30) + FC 16/128
  python train.py --arch tcres8  # TC-ResNet8 (Choi et al. 2019): temporal conv + residual

Steps: waveform augmentation -> C front end (fe.py) -> Keras training ->
full-integer int8 TFLite (CMSIS-NN kernels on the MCU) -> evaluation of the
int8 model -> C arrays for the firmware (firmware/src/voice/model/).
Classes: 0 silence, 1 unknown, 2 wake.
"""
import argparse
import json
import os
import time
from pathlib import Path

import numpy as np

import fe
from make_dataset import NOISE, OUT, SR, read_wav

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
try:  # GPU venv (TF 2.10 + pip CUDA 11): put the CUDA DLLs on the search path first
    import gpu_env  # noqa: F401
except ImportError:
    pass
import tensorflow as tf  # noqa: E402

HERE = Path(__file__).resolve().parent
ART = HERE / "artifacts"
LABELS = ["silence", "unknown", "wake"]


# ---------------------------------------------------------------- augmentation
class Aug:
    def __init__(self, rng):
        self.rng = rng
        self.noise = [read_wav(f) for f in sorted(NOISE.glob("*.wav"))]

    def noise_clip(self, n=SR):
        x = self.noise[self.rng.integers(len(self.noise))]
        o = self.rng.integers(0, len(x) - n)
        return x[o:o + n].astype(np.float32)

    def reverb(self, x):
        rt60 = self.rng.uniform(0.15, 0.6)
        n = int(rt60 * SR)
        t = np.arange(n) / SR
        rir = self.rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
        rir[0] = 1.0
        y = np.convolve(x, rir)[:len(x)]
        return y * (np.abs(x).max() / (np.abs(y).max() + 1e-9))

    def shift(self, x, max_s=0.15):
        s = int(self.rng.uniform(-max_s, max_s) * SR)
        y = np.roll(x, s)
        if s > 0:
            y[:s] = 0
        elif s < 0:
            y[s:] = 0
        return y

    def __call__(self, x, speech=True):
        x = x.astype(np.float32)
        if speech:
            x = self.shift(x)
            if self.rng.random() < 0.3:
                x = self.reverb(x)
            # speaking level: -26 .. +4 dB around the dataset level (far/near talker)
            x = x * 10 ** (self.rng.uniform(-26, 4) / 20)
        if self.rng.random() < 0.85:
            nz = self.noise_clip(len(x))
            p_s = np.mean(x ** 2) + 1e-3
            snr = self.rng.uniform(0, 25)
            g = np.sqrt(p_s / (np.mean(nz ** 2) + 1e-9) / 10 ** (snr / 10)) if speech else \
                10 ** (self.rng.uniform(-50, -5) / 20) * 32767 / (np.abs(nz).max() + 1e-9)
            x = x + g * nz
        x = x + self.rng.normal(0, self.rng.uniform(1, 12), len(x))  # mic self-noise
        return np.clip(x, -32768, 32767).astype(np.int16)


def load_split(split):
    items = []
    for lab in ["wake", "unknown"]:
        for f in sorted((OUT / split / lab).glob("*.wav")):
            items.append((f, LABELS.index(lab)))
    return items


def featurise(items, aug, reps, n_silence, rng, augment=True):
    X, y = [], []
    cache = {}
    for f, lab in items:
        if f not in cache:
            cache[f] = read_wav(f)
        for _ in range(reps.get(lab, 1)):
            pcm = aug(cache[f]) if augment else cache[f].astype(np.int16)
            X.append(fe.mfcc(pcm, fe.NFRAMES))
            y.append(lab)
    for _ in range(n_silence):
        X.append(fe.mfcc(aug(np.zeros(SR), speech=False), fe.NFRAMES))
        y.append(0)
    X = np.stack(X)[..., None]
    y = np.array(y)
    p = rng.permutation(len(y))
    return X[p], y[p]


# ---------------------------------------------------------------- model
ARCH_NAMES = {"s": "ds-cnn-s", "xs": "ds-cnn-xs", "ei": "ei-conv1d", "tiny": "tiny-conv",
              "cnn": "cnn-s", "tcres8": "tc-resnet8"}


def ds_cnn(arch, batch=None):
    ch, blocks = {"s": (64, 4), "xs": (32, 3)}[arch]
    L = tf.keras.layers
    inp = L.Input((fe.NFRAMES, fe.NMFCC, 1), batch_size=batch)
    x = L.Conv2D(ch, (10, 4), strides=(2, 2), padding="same", use_bias=False)(inp)
    x = L.BatchNormalization()(x)
    x = L.ReLU()(x)
    for _ in range(blocks):
        x = L.DepthwiseConv2D((3, 3), padding="same", use_bias=False)(x)
        x = L.BatchNormalization()(x)
        x = L.ReLU()(x)
        x = L.Conv2D(ch, (1, 1), use_bias=False)(x)
        x = L.BatchNormalization()(x)
        x = L.ReLU()(x)
    x = L.Dropout(0.2)(x)
    x = L.AveragePooling2D(pool_size=(x.shape[1], x.shape[2]))(x)
    x = L.Reshape((ch,))(x)  # static shape: Flatten() would emit SHAPE/STRIDED_SLICE ops
    out = L.Dense(len(LABELS), activation="softmax")(x)
    return tf.keras.Model(inp, out, name=f"kws_{arch}")


def ei_conv1d(batch=None):
    """Edge Impulse 'Keyword spotting' default: conv1d 8 -> pool -> conv1d 16 -> pool -> dense.
    Written with (k,1) Conv2D on a (T, 1, C) view so it lowers to CONV_2D + MAX_POOL_2D."""
    L = tf.keras.layers
    inp = L.Input((fe.NFRAMES, fe.NMFCC, 1), batch_size=batch)
    x = L.Reshape((fe.NFRAMES, 1, fe.NMFCC))(inp)
    for ch in (8, 16):
        x = L.Conv2D(ch, (3, 1), padding="same", activation="relu")(x)
        x = L.MaxPooling2D((2, 1), padding="same")(x)
        x = L.Dropout(0.25)(x)
    x = L.Reshape((x.shape[1] * x.shape[3],))(x)
    out = L.Dense(len(LABELS), activation="softmax")(x)
    return tf.keras.Model(inp, out, name="kws_ei")


def tiny_conv(batch=None):
    """TFLM micro_speech 'tiny_conv': one 8-filter 10x8 conv, stride 2, then FC."""
    L = tf.keras.layers
    inp = L.Input((fe.NFRAMES, fe.NMFCC, 1), batch_size=batch)
    x = L.Conv2D(8, (10, 8), strides=(2, 2), padding="same", activation="relu")(inp)
    x = L.Dropout(0.2)(x)
    x = L.Reshape((x.shape[1] * x.shape[2] * x.shape[3],))(x)
    out = L.Dense(len(LABELS), activation="softmax")(x)
    return tf.keras.Model(inp, out, name="kws_tiny")


def cnn_s(batch=None):
    """Hello Edge (Zhang et al. 2017) CNN-S style: conv 28@10x4, conv 30@10x4, low-rank FC."""
    L = tf.keras.layers
    inp = L.Input((fe.NFRAMES, fe.NMFCC, 1), batch_size=batch)
    x = L.Conv2D(28, (10, 4), strides=(2, 2), padding="same", use_bias=False)(inp)
    x = L.ReLU()(L.BatchNormalization()(x))
    x = L.Conv2D(30, (10, 4), strides=(2, 1), padding="same", use_bias=False)(x)
    x = L.ReLU()(L.BatchNormalization()(x))
    x = L.Dropout(0.2)(x)
    x = L.Reshape((x.shape[1] * x.shape[2] * x.shape[3],))(x)
    x = L.Dense(16)(x)                 # linear low-rank layer
    x = L.Dense(128, activation="relu")(x)
    out = L.Dense(len(LABELS), activation="softmax")(x)
    return tf.keras.Model(inp, out, name="kws_cnn")


def tc_resnet8(batch=None):
    """TC-ResNet8 (Choi et al. 2019, width 1.0): MFCC as channels, 9x1 temporal convs."""
    L = tf.keras.layers
    inp = L.Input((fe.NFRAMES, fe.NMFCC, 1), batch_size=batch)
    x = L.Reshape((fe.NFRAMES, 1, fe.NMFCC))(inp)
    x = L.Conv2D(16, (3, 1), padding="same", use_bias=False)(x)
    for ch in (24, 32, 48):
        y = L.Conv2D(ch, (9, 1), strides=(2, 1), padding="same", use_bias=False)(x)
        y = L.ReLU()(L.BatchNormalization()(y))
        y = L.Conv2D(ch, (9, 1), padding="same", use_bias=False)(y)
        y = L.BatchNormalization()(y)
        sc = L.Conv2D(ch, (1, 1), strides=(2, 1), use_bias=False)(x)
        sc = L.ReLU()(L.BatchNormalization()(sc))
        x = L.ReLU()(L.Add()([y, sc]))
    x = L.Dropout(0.2)(x)
    x = L.AveragePooling2D(pool_size=(x.shape[1], 1))(x)
    x = L.Reshape((x.shape[3],))(x)
    out = L.Dense(len(LABELS), activation="softmax")(x)
    return tf.keras.Model(inp, out, name="kws_tcres8")


def build(arch, batch=None):
    if arch in ("s", "xs"):
        return ds_cnn(arch, batch)
    return {"ei": ei_conv1d, "tiny": tiny_conv, "cnn": cnn_s, "tcres8": tc_resnet8}[arch](batch)


def macs(model):
    total = 0
    for l in model.layers:
        if isinstance(l, tf.keras.layers.DepthwiseConv2D):
            o = l.output.shape
            total += o[1] * o[2] * o[3] * l.kernel_size[0] * l.kernel_size[1]
        elif isinstance(l, tf.keras.layers.Conv2D):
            o = l.output.shape
            total += o[1] * o[2] * o[3] * l.kernel_size[0] * l.kernel_size[1] * l.input.shape[3]
        elif isinstance(l, tf.keras.layers.Dense):
            total += l.input.shape[1] * l.units
    return total


# ---------------------------------------------------------------- int8
def to_int8(model, rep):
    # batch fixed to 1: a dynamic batch makes the converter emit SHAPE/PACK ops
    fixed = build(model.name.split("_", 1)[1], batch=1)
    fixed.set_weights(model.get_weights())
    conv = tf.lite.TFLiteConverter.from_keras_model(fixed)
    conv.optimizations = [tf.lite.Optimize.DEFAULT]
    conv.representative_dataset = lambda: ([rep[i:i + 1].astype(np.float32)] for i in range(len(rep)))
    conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    conv.inference_input_type = tf.int8
    conv.inference_output_type = tf.int8
    blob = conv.convert()
    it = tf.lite.Interpreter(model_content=blob)
    used = {d["op_name"] for d in it._get_ops_details()}
    allowed = {"CONV_2D", "DEPTHWISE_CONV_2D", "AVERAGE_POOL_2D", "MAX_POOL_2D", "ADD",
               "RESHAPE", "FULLY_CONNECTED", "SOFTMAX"}
    assert used <= allowed, f"ops not registered in kws_engine.cpp: {used - allowed}"
    return blob


class Int8Model:
    def __init__(self, blob):
        self.it = tf.lite.Interpreter(model_content=blob)
        self.it.allocate_tensors()
        self.i = self.it.get_input_details()[0]
        self.o = self.it.get_output_details()[0]
        self.in_scale, self.in_zp = self.i["quantization"]
        self.out_scale, self.out_zp = self.o["quantization"]

    def quant(self, feats):
        q = np.round(feats / self.in_scale + self.in_zp)
        return np.clip(q, -128, 127).astype(np.int8)

    def __call__(self, feats):  # (49, 10) float -> probabilities (3,)
        self.it.set_tensor(self.i["index"], self.quant(feats)[None, ..., None])
        self.it.invoke()
        q = self.it.get_tensor(self.o["index"])[0].astype(np.float32)
        return (q - self.out_zp) * self.out_scale

    def raw(self, feats):
        self.it.set_tensor(self.i["index"], self.quant(feats)[None, ..., None])
        self.it.invoke()
        return self.it.get_tensor(self.o["index"])[0]


def evaluate(m8, X, y, thr):
    P = np.stack([m8(x[..., 0]) for x in X])
    pred = P.argmax(1)
    acc = float((pred == y).mean())
    wake = y == 2
    frr = float((P[wake, 2] < thr).mean()) if wake.any() else 0.0
    far = float((P[~wake, 2] >= thr).mean())
    cm = np.zeros((3, 3), int)
    for t, p in zip(y, pred):
        cm[t, p] += 1
    return {"accuracy": acc, "frr": frr, "far_per_clip": far, "confusion": cm.tolist(),
            "n": int(len(y)), "n_wake": int(wake.sum())}


# ---------------------------------------------------------------- export
def c_array(name, blob, align=16):
    rows = [", ".join(f"0x{b:02x}" for b in blob[i:i + 16]) for i in range(0, len(blob), 16)]
    return (f"alignas({align}) const unsigned char {name}[] = {{\n  " + ",\n  ".join(rows) +
            f"\n}};\nconst unsigned int {name}_len = {len(blob)};\n")


def export(arch, blob, m8, report):
    d = HERE.parent / "pico2" / "firmware" / "src" / "voice" / "model"
    d.mkdir(exist_ok=True)
    (d / f"kws_{arch}.cc").write_text(
        f"// Generated by embedded/kws/train.py - {ARCH_NAMES[arch]} \"Hey Edgie\", int8.\n"
        f"// test accuracy {report['test']['accuracy']:.4f}, {report['macs']} MACs, "
        f"{report['params']} parameters.\n#include <stdalign.h>\n#include \"kws_models.h\"\n\n"
        + c_array(f"kws_model_{arch}", blob)
        + f"const struct kws_model_info kws_model_{arch}_info = {{\n"
          f"  \"{ARCH_NAMES[arch]}\", kws_model_{arch}, {len(blob)},\n"
          f"  {m8.in_scale:.9g}f, {int(m8.in_zp)}, {m8.out_scale:.9g}f, {int(m8.out_zp)},\n"
          f"  {report['macs']}u, {report['params']}u,\n}};\n", encoding="utf-8")


BENCH_ORDER = ["s", "xs", "ei", "tiny", "cnn", "tcres8"]  # = kws_bench_models[] on the MCU


def export_bench_clips(m_by_arch, rng):
    """A few held-out test clips in flash so the MCU can be benchmarked with no mic."""
    pick = []
    for lab, pat, n in [("wake", "sapi_*", 2), ("wake", "en_*", 4), ("unknown", "en_*", 2),
                        ("unknown", "sapi_*", 2), ("unknown", "sc_*", 3)]:
        files = sorted((OUT / "test" / lab).glob(pat + ".wav"))
        rng.shuffle(files)
        pick += [(f, LABELS.index(lab)) for f in files[:n]]
    aug = Aug(np.random.default_rng(7))
    pick.append((None, 0))  # silence: factory noise only
    d = HERE.parent / "pico2" / "firmware" / "src" / "voice" / "model"
    lines = ["// Generated by embedded/kws/train.py: held-out test clips for 'eaa kws bench'.",
             "// expected[] = int8 outputs of the host TFLite interpreter for the same input.",
             "#include \"kws_models.h\"\n"]
    entries = []
    for k, (f, lab) in enumerate(pick):
        pcm = read_wav(f).astype(np.int16) if f else aug(np.zeros(SR), speech=False)
        feats = fe.mfcc(pcm, fe.NFRAMES)
        exp = {a: m.raw(feats).tolist() for a, m in m_by_arch.items()}
        name = f.stem if f else "factory_noise"
        lines.append(f"static const int16_t clip{k}[16000] = {{" +
                     ",".join(str(int(v)) for v in pcm[:SR]) + "};")
        rows = ", ".join("{" + ",".join(map(str, exp.get(a, [0, 0, 0]))) + "}" for a in BENCH_ORDER)
        entries.append(f"  {{\"{name[:40]}\", {lab}, clip{k}, {{{rows}}}}},")
    lines.append("const struct kws_model_info *const kws_bench_models[KWS_BENCH_NMODELS] = {\n" +
                 ",\n".join(f"  &kws_model_{a}_info" for a in BENCH_ORDER) + "\n};")
    lines.append("const struct kws_bench_clip kws_bench_clips[] = {\n" + "\n".join(entries) + "\n};")
    lines.append(f"const unsigned int kws_bench_clip_count = {len(pick)};")
    (d / "kws_bench_clips.cc").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", default="s", choices=list(ARCH_NAMES))
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--thr", type=float, default=0.8)
    ap.add_argument("--bench-clips", action="store_true")
    ap.add_argument("--no-export", action="store_true",
                    help="trial run: write artifacts/trial_* only, keep the firmware models")
    a = ap.parse_args()
    gpus = tf.config.list_physical_devices("GPU")
    for g in gpus:
        tf.config.experimental.set_memory_growth(g, True)
    print("TensorFlow", tf.__version__, "on", "GPU " + gpus[0].name if gpus else "CPU only")
    rng = np.random.default_rng(42)
    tf.keras.utils.set_random_seed(42)
    ART.mkdir(exist_ok=True)

    cache = ART / "features.npz"
    if cache.exists():
        z = np.load(cache)
        Xtr, ytr, Xva, yva, Xte, yte = (z[k] for k in ["Xtr", "ytr", "Xva", "yva", "Xte", "yte"])
    else:
        t0 = time.time()
        aug = Aug(rng)
        tr, va, te = load_split("train"), load_split("val"), load_split("test")
        Xtr, ytr = featurise(tr, aug, {2: 3, 1: 1}, 4000, rng)
        Xva, yva = featurise(va, aug, {2: 2, 1: 1}, 500, rng)
        Xte, yte = featurise(te, aug, {2: 4, 1: 1}, 500, rng)  # augmented held-out voices
        np.savez_compressed(cache, Xtr=Xtr, ytr=ytr, Xva=Xva, yva=yva, Xte=Xte, yte=yte)
        print(f"features in {time.time() - t0:.0f} s")
    print("train", np.bincount(ytr), "val", np.bincount(yva), "test", np.bincount(yte))

    model = build(a.arch)
    n_macs, n_params = macs(model), model.count_params()
    print(f"{model.name}: {n_params} params, {n_macs} MACs")
    cw = {i: len(ytr) / (3 * c) for i, c in enumerate(np.bincount(ytr))}
    sched = tf.keras.optimizers.schedules.CosineDecay(3e-3, a.epochs * (len(ytr) // 128 + 1))
    model.compile(optimizer=tf.keras.optimizers.Adam(sched), loss="sparse_categorical_crossentropy", metrics=["accuracy"])
    t0 = time.time()
    hist = model.fit(Xtr, ytr, batch_size=128, epochs=a.epochs, validation_data=(Xva, yva),
                     class_weight=cw, verbose=2)
    train_s = time.time() - t0

    blob = to_int8(model, Xtr[rng.choice(len(Xtr), 500, replace=False)])
    pre = "trial_" if a.no_export else ""
    (ART / f"{pre}kws_{a.arch}.tflite").write_bytes(blob)
    model.save_weights(str(ART / f"{pre}kws_{a.arch}.weights.h5"))
    m8 = Int8Model(blob)
    float_acc = float(model.evaluate(Xte, yte, verbose=0)[1])
    rep = {"arch": a.arch, "params": int(n_params), "macs": int(n_macs),
           "tflite_bytes": len(blob), "train_seconds": round(train_s),
           "val_acc_curve": [round(v, 4) for v in hist.history["val_accuracy"]],
           "test_float_accuracy": float_acc, "threshold": a.thr,
           "val": evaluate(m8, Xva, yva, a.thr), "test": evaluate(m8, Xte, yte, a.thr),
           "input_quant": [float(m8.in_scale), int(m8.in_zp)],
           "output_quant": [float(m8.out_scale), int(m8.out_zp)]}
    rep["device"] = "GPU" if gpus else "CPU"
    rep["tensorflow"] = tf.__version__
    (ART / f"{pre}report_{a.arch}.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: v for k, v in rep.items() if k != "val_acc_curve"}, indent=1))
    if a.no_export:
        return
    export(a.arch, blob, m8, rep)
    if a.bench_clips:
        ms = {}
        for arch in BENCH_ORDER:
            p = ART / f"kws_{arch}.tflite"
            if p.exists():
                ms[arch] = Int8Model(p.read_bytes())
        export_bench_clips(ms, np.random.default_rng(3))


if __name__ == "__main__":
    main()
