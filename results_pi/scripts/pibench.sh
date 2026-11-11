cd ~/llama/build/bin
for m in SmolLM2-135M-Instruct.Q4_K_M Qwen2.5-0.5B-Instruct.Q4_K_M Qwen3-0.6B.Q4_K_M Qwen3-0.6B.Q8_0 Gemma-3-1B-it.Q4_K_M; do
  echo "### $m $(md5sum < ~/models/$m.gguf | cut -c1-32)"
  ./llama-bench -m ~/models/$m.gguf -t 4 -p 128 -n 64 -r 3 > /tmp/b.out 2>&1 & pid=$!
  peak=0; while kill -0 $pid 2>/dev/null; do r=$(awk "/VmRSS/{print \$2}" /proc/$pid/status 2>/dev/null); [ -n "$r" ] && [ "$r" -gt "$peak" ] && peak=$r; sleep 0.5; done
  grep -E "^\|" /tmp/b.out | tail -2; echo "peak_rss_mib=$((peak/1024))"
done
echo ALLDONE
