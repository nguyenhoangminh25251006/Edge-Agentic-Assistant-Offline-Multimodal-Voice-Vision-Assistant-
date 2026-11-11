# Raspberry Pi benchmark (llama-bench)

Đo ngày 2026-11-11 trên Pi `192.168.2.61`. Số liệu chính thức nằm trong `pi_bench.csv`, lấy từ `logs/pibench_split.log`.

## Cấu trúc thư mục
```
results_pi/
├── README.md               tài liệu này
├── pi_bench.csv            bảng kết quả chính thức (6 model)
├── logs/
│   ├── pibench_split.log   lần đo chính thức (pp128 và tg64 tách riêng)
│   ├── pibench_seq.log     lần chạy tuần tự sau khi rút phím/chuột (dừng ở Qwen2.5 vì crash)
│   ├── pibench_seq.prev.log lần chạy tuần tự trước đó, khi còn sụt áp (cũng dừng ở Qwen2.5)
│   ├── pibench.log         lần đo đầu tiên, KHÔNG HỢP LỆ (Pi bị sụt áp, số thấp ~2-3 lần)
│   ├── pibench2.log        lần đo Arch-Agent đầu tiên, KHÔNG HỢP LỆ (sụt áp)
│   └── server.log          thử chạy llama-server với Arch-Agent Q3_K_S
├── scripts/                các script đã chạy trên Pi (pibench_split.sh là script chính thức)
├── build/                  log build llama.cpp trên Pi
└── system/
    ├── pi_env.txt          model Pi, kernel, RAM, cấu hình xung, thiết bị USB
    ├── config.txt          /boot/firmware/config.txt (arm_freq=1400, over_voltage=2)
    ├── llama_build_and_models.txt  cờ CMake, phiên bản llama-bench, danh sách + md5 file model
    └── dmesg_power.txt     trích log sụt áp (Pi đã khởi động lại, log gốc đã mất)
```

## Thiết bị
- Raspberry Pi 3 Model B Rev 1.2 (rev `a02082`, không phải 3B+), 4 nhân Cortex-A53, RAM 905 MiB, swap 904 MiB.
- Kernel 6.18.50+rpt-rpi-v8, aarch64. llama.cpp build Release, `GGML_NATIVE=ON`, CPU backend.
- **Đang ép xung:** `/boot/firmware/config.txt` đặt `arm_freq=1400`, `over_voltage=2`
  (xung chuẩn của Pi 3B là 1200 MHz). Mọi số liệu dưới đây được đo ở 1400 MHz.
- Đã rút phím, chuột, HDMI, quạt để giảm tải nguồn.

## Phương pháp (`scripts/pibench_split.sh`)
- `llama-bench -t 4 -r 3`; pp128 (`-p 128 -n 0`) và tg64 (`-p 0 -n 64`) chạy thành 2 lần riêng.
- Đo tuần tự từng model, nghỉ 30 s giữa các lần, trước mỗi lần chờ hết cờ throttle hiện tại.
- Chỉ nhận kết quả khi: xung ARM luôn ở max (lấy mẫu mỗi 0.5 s), mã thoát 0, có dòng kết quả.
  Không đạt thì đo lại, tối đa 4 lần. Crash do tín hiệu 2 lần thì dừng hẳn.

## Kết quả

| Model | pp128 (t/s) | tg64 (t/s) | RAM cao nhất |
|---|---|---|---|
| SmolLM2-135M Q4_K_M | 34.50 ± 1.59 | 16.27 ± 0.52 | 137 MiB |
| Qwen2.5-0.5B Q4_K_M | 11.43 ± 0.10 | 4.85 ± 0.09 | 442 MiB |
| Qwen3-0.6B Q4_K_M | 10.47 ± 0.09 | 4.58 ± 0.09 | 549 MiB |
| Qwen3-0.6B Q8_0 | — | — | ~766 MiB |
| Arch-Agent-1.5B Q3_K_S | 3.08 ± 0.01 | 2.14 ± 0.00 | 745 MiB |
| Arch-Agent-1.5B Q3_K_M | 3.28 ± 0.02 | — | 767 MiB |

- Qwen3-0.6B Q8_0: cả 8 lần đo đều bị hạ xung xuống 600–800 MHz giữa chừng, không có lần nào sạch.
- Arch-Agent Q3_K_M tg64: crash `Illegal instruction`. Cả 2 lần crash đều xảy ra đúng lúc xung
  tụt xuống 600 MHz, nên nguyên nhân có thể là nguồn yếu cộng với ép xung/tăng áp.
