// Простой SVG-график тренда (без библиотек). Одна форма на одно применение.
import type { Timeline } from "./api";

export default function TrendChart({ timeline }: { timeline: Timeline }) {
  const values = timeline.runs.map((r) => r.total_score);
  if (values.length < 2) {
    return <small>Недостаточно данных (нужно 2+ скрининга)</small>;
  }
  const w = 560;
  const h = 140;
  const pad = 24;
  const max = Math.max(20, ...values);
  const px = (i: number) => pad + (i * (w - pad * 2)) / (values.length - 1);
  const py = (v: number) => h - pad - (v * (h - pad * 2)) / max;
  const line = values.map((v, i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(" ");
  const last = values[values.length - 1];
  const delta = timeline.trend.delta_30;
  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: "auto" }}>
        <polyline
          points={line}
          fill="none"
          stroke={last >= 15 ? "#d64545" : "#2f6fed"}
          strokeWidth={2.5}
          strokeLinejoin="round"
        />
      </svg>
      <small>
        Последний балл: {last}. Дельта за 30 дней: {delta === null ? "—" : delta > 0 ? `+${delta}` : delta}
      </small>
    </div>
  );
}