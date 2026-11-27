interface SparklineProps {
  values: Array<number | null | undefined>;
  width?: number;
  height?: number;
  stroke?: string;
}

export function Sparkline({ values, width = 96, height = 22, stroke = "#16a34a" }: SparklineProps) {
  const nums = values.filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  if (nums.length < 2) {
    return (
      <svg width={width} height={height} className="overflow-visible" aria-hidden="true">
        <line x1={0} y1={height / 2} x2={width} y2={height / 2} stroke="#cbd5e1" strokeWidth={1.5} strokeDasharray="3 3" />
      </svg>
    );
  }
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const pts = nums
    .map((v, i) => {
      const x = (i / (nums.length - 1)) * width;
      const y = height - 2 - ((v - min) / span) * (height - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="overflow-visible" aria-hidden="true">
      <polyline fill="none" stroke={stroke} strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" points={pts} />
    </svg>
  );
}
