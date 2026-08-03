import type { SignalHistoryPoint } from "../../shared/contracts";

function points(values: number[], width = 320, height = 80): string {
  if (values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  return values.map((value, index) => {
    const x = index / (values.length - 1) * width;
    const y = height - ((value - min) / range * (height - 8) + 4);
    return `${x},${y}`;
  }).join(" ");
}

export function MiniHistoryChart({ history, field, label }: {
  history: SignalHistoryPoint[];
  field: keyof SignalHistoryPoint;
  label: string;
}) {
  const values = history.map((item) => item[field]).filter((value): value is number => typeof value === "number");
  if (values.length < 2) return <div className="empty compact">Нет реальной истории для {label}</div>;
  return <div className="spark-card"><span>{label}</span><svg viewBox="0 0 320 80" role="img" aria-label={`${label} over cycles`} preserveAspectRatio="none"><polyline points={points(values)} /></svg></div>;
}
