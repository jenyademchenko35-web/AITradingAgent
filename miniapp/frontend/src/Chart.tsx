import { createChart, CandlestickSeries, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { Candle } from "../../shared/contracts";

export function SignalChart({ candles }: { candles: Candle[] }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current || candles.length === 0) return;
    const chart = createChart(host.current, {
      height: 230,
      layout: { background: { color: "transparent" }, textColor: "#91a0b8" },
      grid: { vertLines: { color: "#182132" }, horzLines: { color: "#182132" } },
      rightPriceScale: { borderColor: "#27334a" },
      timeScale: { borderColor: "#27334a", timeVisible: true },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#21d09a", downColor: "#ff5d72", borderVisible: false,
      wickUpColor: "#21d09a", wickDownColor: "#ff5d72",
    });
    series.setData(candles.map((candle) => ({
      ...candle,
      time: (typeof candle.time === "number"
        ? candle.time
        : Math.floor(new Date(candle.time).getTime() / 1000)) as UTCTimestamp,
    })));
    chart.timeScale().fitContent();
    const observer = new ResizeObserver(([entry]) => chart.applyOptions({ width: entry.contentRect.width }));
    observer.observe(host.current);
    return () => { observer.disconnect(); chart.remove(); };
  }, [candles]);
  return candles.length ? <div className="chart" ref={host} aria-label="График сигнала" /> : <div className="empty">Свечи пока недоступны</div>;
}
