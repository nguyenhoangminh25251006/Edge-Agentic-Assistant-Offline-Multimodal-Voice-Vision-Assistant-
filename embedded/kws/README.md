# Wake word "Hey Edgie" — training toolkit

Full design, results and benchmark: [WAKEWORD_REPORT.md](WAKEWORD_REPORT.md).

| File | What it does |
|---|---|
| `fe.py` | Builds `pico2/firmware/src/voice/kws_frontend.c` as a host DLL (zig cc) and exposes it to Python, so training uses exactly the MCU's MFCC. `python fe.py` checks it against a numpy reference. |
| `make_dataset.py` | Synthesises positives with Piper TTS (904 LibriTTS-R speakers), negatives from Google Speech Commands v2 + TTS confusables + factory phrases, and noise (Speech Commands noise + motor hum). |
| `train.py` | Augmentation → features → DS-CNN (Keras) → int8 TFLite → evaluation → C arrays in `pico2/firmware/src/voice/model/`. |
| `eval_stream.py` | Line-by-line Python copy of `kws_pipeline.c` (VAD gate, stride, smoothing, refractory) on long held-out streams: detection rate, false wakes per hour, VAD savings. |

```powershell
py -3.12 -m venv $HOME\kws\.venv; & $HOME\kws\.venv\Scripts\pip install "tensorflow==2.18.*" numpy scipy piper-tts
# Piper voices -> $HOME\kws\voices, Speech Commands v2 -> $HOME\kws\data\sc  (see WAKEWORD_REPORT.md §4)
python make_dataset.py --shards 3 --shard 0   # (run shards 0..2 in parallel)
python train.py --arch xs
python train.py --arch s --bench-clips
python eval_stream.py --arch s
```

Data and artifacts live in `%USERPROFILE%\kws` (not in git, ~3 GB).
