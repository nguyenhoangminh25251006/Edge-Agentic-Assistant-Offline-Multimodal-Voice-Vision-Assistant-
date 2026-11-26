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
python eval_stream.py --arch s
```

Training runs on the GPU (NVIDIA, e.g. the GTX 1060 6 GB on the dev PC), not on the CPU. TensorFlow 2.11+ has no
native-Windows GPU build, so training uses a separate TF 2.10 venv with the CUDA 11 / cuDNN 8 pip wheels;
`gpu_env.py` (imported by `train.py`) puts their DLLs on the search path.

```powershell
uv venv -p 3.10 $HOME\kws\.venv-gpu
uv pip install -p $HOME\kws\.venv-gpu\Scripts\python.exe "tensorflow==2.10.1" "numpy<2" scipy nvidia-cuda-runtime-cu11 nvidia-cublas-cu11 nvidia-cufft-cu11 nvidia-curand-cu11 nvidia-cusolver-cu11 nvidia-cusparse-cu11 "nvidia-cudnn-cu11==8.9.*"
& $HOME\kws\.venv-gpu\Scripts\python.exe train.py --arch s     # prints "TensorFlow 2.10.1 on GPU ..."
& $HOME\kws\.venv-gpu\Scripts\python.exe train.py --arch tcres8 --no-export   # trial: keeps the firmware models
# comparison set: --arch s | xs | ei | tiny | cnn | tcres8  (WAKEWORD_REPORT.md §3.4); --bench-clips refreshes the MCU bench data
```

Data and artifacts live in `%USERPROFILE%\kws` (not in git, ~3 GB).
