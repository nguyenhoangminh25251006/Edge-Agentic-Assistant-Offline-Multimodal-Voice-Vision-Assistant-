"""Streaming evaluation of the wake-word pipeline exactly as kws_pipeline.c runs it.

  python eval_stream.py --arch s [--thr 80] [--stride 3] [--smooth 3]

Builds long test streams from held-out clips (speakers / voices never used in
training), runs MFCC every 20 ms, the energy VAD gate, the int8 model every
STRIDE hops, score smoothing, threshold and refractory - a line-by-line Python
copy of firmware/src/voice/kws_pipeline.c - and reports:
  * detection rate of "Hey Edgie" inside the stream (false rejects),
  * false wakes per hour of non-stop speech + noise (worst case: real rooms
    have far less speech than this stream),
  * the fraction of model runs the VAD saves in a mostly quiet room.
Also sweeps the threshold to draw the FRR / FA-per-hour trade-off.
"""
import argparse
import json
from pathlib import Path

import numpy as np

import fe
from make_dataset import OUT, SR, read_wav
from train import ART, Aug, Int8Model

HOP, NF = fe.HOP, fe.NFRAMES


def build_stream(rng, aug, with_wake, quiet_ratio=0.0, minutes=None):
    """Concatenate held-out clips with random gaps over factory noise.
    Returns int16 pcm and a list of (start, end) sample spans of wake words."""
    neg = sorted((OUT / "test" / "unknown").glob("*.wav"))
    pos = sorted((OUT / "test" / "wake").glob("*.wav"))
    clips = [(f, 0) for f in neg]
    if with_wake:
        clips += [(f, 1) for f in pos]
    rng.shuffle(clips)
    if minutes:
        clips = clips[:int(minutes * 60 / (1.0 + 0.6 + quiet_ratio * 10))]
    parts, spans, pos_s = [], [], 0
    for f, is_wake in clips:
        x = read_wav(f)
        gap = int(rng.uniform(0.3, 0.9) * SR + quiet_ratio * rng.uniform(5, 15) * SR)
        g = aug(np.zeros(gap), speech=False).astype(np.float32) * 0.3
        y = aug(x).astype(np.float32)
        if is_wake:
            spans.append((pos_s + gap, pos_s + gap + len(y)))
        parts += [g, y]
        pos_s += gap + len(y)
    pcm = np.clip(np.concatenate(parts), -32768, 32767).astype(np.int16)
    return pcm, spans


def run_pipeline(pcm, m8, thr, stride, smooth, vad_db=9.0, vad=True):
    padded = np.concatenate([np.zeros(fe.FRAME - HOP, np.int16), pcm])
    feats = fe.mfcc(padded)                        # frame f == streaming hop f + 1
    frames = np.lib.stride_tricks.sliding_window_view(padded.astype(np.float32) / 32768,
                                                      fe.FRAME)[::HOP]
    db = 10 * np.log10((frames ** 2).mean(1) + 1e-10)
    q = m8.quant(feats)
    nf, hang, refr, hist = -60.0, 0, 0, [0] * smooth
    hi, detections, runs, gated = 0, [], 0, 0
    scores = np.zeros(len(feats), np.float32)
    for f in range(len(feats)):
        nf += (0.3 if db[f] < nf else 0.004) * (db[f] - nf)
        nf = max(nf, -90.0)
        if db[f] > nf + vad_db and db[f] > -70:
            hang = NF
        elif hang > 0:
            hang -= 1
        if refr > 0:
            refr -= 1
        if (f + 1) % stride != 0 or f + 1 < NF:
            continue
        if vad and hang == 0:
            gated += 1
            hist = [0] * smooth
            continue
        m8.it.set_tensor(m8.i["index"], q[f + 1 - NF:f + 1][None, ..., None])
        m8.it.invoke()
        o = m8.it.get_tensor(m8.o["index"])[0].astype(np.float32)
        p = (o[2] - m8.out_zp) * m8.out_scale
        runs += 1
        hist[hi] = int(np.clip(np.rint(p * 100), 0, 100))
        hi = (hi + 1) % smooth
        s = sum(hist) // smooth
        scores[f] = s
        if s >= thr and refr == 0:
            refr = 75
            hist = [0] * smooth
            detections.append((f * HOP + fe.FRAME - (fe.FRAME - HOP), s))  # sample index
    return detections, runs, gated, scores


def score_stream(dets, spans, tol=int(0.8 * SR)):
    hit = [False] * len(spans)
    fa = 0
    for t, _ in dets:
        k = next((i for i, (a, b) in enumerate(spans) if a <= t <= b + tol), None)
        if k is None:
            fa += 1
        else:
            hit[k] = True
    return sum(hit), fa


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", default="s")
    ap.add_argument("--thr", type=int, default=80)
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--smooth", type=int, default=3)
    a = ap.parse_args()
    m8 = Int8Model((ART / f"kws_{a.arch}.tflite").read_bytes())
    rng = np.random.default_rng(11)
    aug = Aug(np.random.default_rng(5))

    # 1) mixed stream: wake words between confusables / commands / noise
    pcm, spans = build_stream(rng, aug, with_wake=True)
    hours = len(pcm) / SR / 3600
    res = {"arch": a.arch, "stride": a.stride, "smooth": a.smooth,
           "mixed_stream_minutes": round(hours * 60, 1), "n_wake": len(spans), "sweep": []}
    # one pass with threshold 0 to log scores, then sweep thresholds offline
    for thr in [50, 60, 70, 80, 90, 95]:
        dets, runs, gated, _ = run_pipeline(pcm, m8, thr, a.stride, a.smooth)
        hits, fa = score_stream(dets, spans)
        res["sweep"].append({"thr": thr, "detected": hits, "frr": round(1 - hits / len(spans), 4),
                             "false_wakes": fa, "fa_per_hour": round(fa / hours, 2)})
        print(res["sweep"][-1])
    # 2) quiet room: 10 % speech -> how often does the model run at all?
    pcm_q, _ = build_stream(np.random.default_rng(12), aug, with_wake=False,
                            quiet_ratio=1.0, minutes=30)
    dets, runs, gated, _ = run_pipeline(pcm_q, m8, a.thr, a.stride, a.smooth)
    res["quiet_room"] = {"minutes": round(len(pcm_q) / SR / 60, 1), "model_runs": runs,
                         "vad_skipped": gated, "run_fraction": round(runs / max(1, runs + gated), 3),
                         "false_wakes": len(dets)}
    print(res["quiet_room"])
    (ART / f"stream_{a.arch}.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
