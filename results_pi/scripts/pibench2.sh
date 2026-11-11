cd ~/llama/build/bin
for m in Arch-Agent-1.5B.Q3_K_S-e4 Arch-Agent-1.5B.Q3_K_M-e4; do
  echo "### $m"
  ./llama-bench -m ~/models/$m.gguf -t 4 -p 128 -n 64 -r 3 > /tmp/b.out 2>&1 & pid=$!
  peak=0; while kill -0 $pid 2>/dev/null; do r=$(awk "/VmRSS/{print \$2}" /proc/$pid/status 2>/dev/null); [ -n "$r" ] && [ "$r" -gt "$peak" ] && peak=$r; sleep 0.5; done
  grep -E "^\|" /tmp/b.out | tail -2; echo "peak_rss_mib=$((peak/1024))"; free -m | sed -n 3p
done
vcgencmd get_throttled
echo ALLDONE
