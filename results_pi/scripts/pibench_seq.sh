#!/bin/bash
# Bench từng model một: chạy xong, tắt hẳn, nghỉ rồi mới sang model sau.
# Chấp nhận nguồn lúc sụt lúc không, NHƯNG chỉ giữ kết quả khi xung ARM luôn ở max (1400 MHz)
# suốt lần đo: lấy mẫu xung mỗi 0.5 s; nếu có mẫu nào < max (firmware hạ xung do sụt áp/nhiệt)
# thì bỏ kết quả và đo lại model đó (tối đa TRIES lần).
BIN=~/llama/build/bin/llama-bench
MODELS=(
  SmolLM2-135M-Instruct.Q4_K_M
  Qwen2.5-0.5B-Instruct.Q4_K_M
  Qwen3-0.6B.Q4_K_M
  Qwen3-0.6B.Q8_0
  Arch-Agent-1.5B.Q3_K_S-e4
  Arch-Agent-1.5B.Q3_K_M-e4
)
REST=30
TRIES=4
CRASH=0
MAXHZ=$(( $(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq) * 1000 ))
uvcount() { dmesg | grep -c "Undervoltage detected"; }
clk() { vcgencmd measure_clock arm | cut -d= -f2; }

wait_full_clock() {   # chờ tới khi không còn bị hạ xung (bit 1 = đang cap, bit 2 = đang throttle)
  for _ in $(seq 120); do
    t=$(vcgencmd get_throttled | cut -d= -f2)
    (( (t & 0x7) == 0 )) && return 0
    sleep 1
  done
  return 0
}

for m in "${MODELS[@]}"; do
  f=~/models/$m.gguf
  [ -f "$f" ] || { echo "### $m MISSING"; continue; }
  echo "### $m"
  for try in $(seq $TRIES); do
    pkill -x llama-bench; pkill -x llama-server; pkill -x llama-cli
    while pgrep -x 'llama-(bench|server|cli)' >/dev/null; do sleep 1; done
    sync
    wait_full_clock
    uv0=$(uvcount)
    out=$(mktemp)
    $BIN -m "$f" -p 128 -n 64 -r 3 -t 4 -o md > "$out" 2>/dev/null &
    pid=$!; sleep 0.3; peak=0; minclk=$MAXHZ; low=0; n=0
    while kill -0 "$pid" 2>/dev/null; do
      r=$(awk '/VmRSS/{print $2}' /proc/$pid/status 2>/dev/null); [ -n "$r" ] && (( r > peak )) && peak=$r
      c=$(clk); n=$((n+1))
      (( c < minclk )) && minclk=$c
      (( c < MAXHZ - 1000000 )) && low=$((low+1))
      sleep 0.5
    done
    wait $pid; rc=$?
    if (( rc >= 128 )); then CRASH=$((CRASH+1)); echo "CRASH rc=$rc total=$CRASH"; (( CRASH >= 2 )) && { echo "ABORT: 2 crashes, likely power"; exit 2; }; fi
    rows=$(grep -cE "^\| [a-z]" "$out")
    uv=$(( $(uvcount) - uv0 ))
    echo "try=$try rc=$rc rows=$rows samples=$n low_clock_samples=$low min_clock_mhz=$((minclk/1000000)) new_undervoltage=$uv peak_rss_mib=$((peak/1024)) $(vcgencmd measure_temp)"
    if (( low == 0 && rc == 0 && rows >= 3 )); then
      grep -E '^\| [a-z]' "$out" | grep -v '^| model'
      echo "RESULT_OK"
      rm -f "$out"; break
    fi
    echo "DISCARD (clock<max or crash rc=$rc)"
    rm -f "$out"
    sleep $REST
  done
  free -m | grep Swap
  sleep $REST
done
echo ALLDONE
