#!/bin/bash
# Bench từng model, mỗi model đo pp128 và tg64 thành 2 lần chạy riêng (nghỉ giữa các lần)
# để rút ngắn thời gian tải nặng liên tục. Chỉ giữ kết quả khi: xung luôn max, rc=0, có dòng kết quả.
# Crash do tín hiệu (rc>=128) tổng cộng 2 lần -> dừng hẳn (bảo vệ thẻ SD).
BIN=~/llama/build/bin/llama-bench
MODELS=(
  SmolLM2-135M-Instruct.Q4_K_M
  Qwen2.5-0.5B-Instruct.Q4_K_M
  Qwen3-0.6B.Q4_K_M
  Qwen3-0.6B.Q8_0
  Arch-Agent-1.5B.Q3_K_S-e4
  Arch-Agent-1.5B.Q3_K_M-e4
)
TESTS=("-p 128 -n 0" "-p 0 -n 64")
REST=30
TRIES=4
CRASH=0
MAXHZ=$(( $(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq) * 1000 ))
uvcount() { dmesg | grep -c "Undervoltage detected"; }
clk() { vcgencmd measure_clock arm | cut -d= -f2; }
wait_full_clock() {
  local th
  for _ in $(seq 120); do
    th=$(vcgencmd get_throttled | cut -d= -f2)
    (( (th & 0x7) == 0 )) && return 0
    sleep 1
  done
}

for m in "${MODELS[@]}"; do
  f=~/models/$m.gguf
  [ -f "$f" ] || { echo "### $m MISSING"; continue; }
  echo "### $m"
  for t in "${TESTS[@]}"; do
    echo "## test: $t"
    ok=0
    for try in $(seq $TRIES); do
      pkill -x llama-bench; pkill -x llama-server; pkill -x llama-cli
      while pgrep -x 'llama-(bench|server|cli)' >/dev/null; do sleep 1; done
      sync
      wait_full_clock
      uv0=$(uvcount)
      out=$(mktemp)
      $BIN -m "$f" $t -r 3 -t 4 -o md > "$out" 2>/dev/null &
      pid=$!; sleep 0.3; peak=0; minclk=$MAXHZ; low=0; n=0
      while kill -0 "$pid" 2>/dev/null; do
        r=$(awk '/VmRSS/{print $2}' /proc/$pid/status 2>/dev/null); [ -n "$r" ] && (( r > peak )) && peak=$r
        c=$(clk); n=$((n+1))
        (( c < minclk )) && minclk=$c
        (( c < MAXHZ - 1000000 )) && low=$((low+1))
        sleep 0.5
      done
      wait $pid; rc=$?
      (( rc != 0 && n == 0 )) && { echo "ABORT: llama-bench exited immediately rc=$rc (bad args?)"; rm -f "$out"; exit 3; }
      rows=$(grep -E "^\| [a-z]" "$out" | grep -vc '^| model')
      uv=$(( $(uvcount) - uv0 ))
      echo "try=$try rc=$rc rows=$rows samples=$n low_clock_samples=$low min_clock_mhz=$((minclk/1000000)) new_undervoltage=$uv peak_rss_mib=$((peak/1024)) $(vcgencmd measure_temp)"
      if (( rc >= 128 )); then
        CRASH=$((CRASH+1)); echo "CRASH rc=$rc total=$CRASH"
        (( CRASH >= 2 )) && { echo "ABORT: 2 crashes"; rm -f "$out"; exit 2; }
      fi
      if (( low == 0 && rc == 0 && rows >= 1 )); then
        grep -E '^\| [a-z]' "$out" | grep -v '^| model'
        echo "RESULT_OK"; ok=1
        rm -f "$out"; break
      fi
      echo "DISCARD"
      rm -f "$out"
      sleep $REST
    done
    (( ok )) || echo "FAILED $m $t"
    sleep $REST
  done
  free -m | grep Swap
done
echo ALLDONE
