import { useEffect, useRef, useState } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { EMPTY_PHONES } from '../../lib/empty';
import { roleHex } from '../../lib/roleColors';
import { loadBoolPref, savePref } from '../../state/prefs';
import { useApp, useSnapshot } from '../../state/store';

const WINDOW_S = 10; // last 10 seconds

function magnitude(acc: ArrayLike<number>): number {
  const x = acc[0] ?? 0;
  const y = acc[1] ?? 0;
  const z = acc[2] ?? 0;
  return Math.sqrt(x * x + y * y + z * z);
}

export function chartOptions(
  width: number,
  height: number,
  ids: string[],
  roles: Map<string, string>,
): uPlot.Options {
  return {
    width: Math.max(width, 100),
    height: Math.max(height, 80),
    padding: [4, 4, 0, 0],
    scales: { x: { time: true } },
    // The role legend is rendered by React (swatches only); uPlot's own legend
    // spent a row on "Time: --" and repeated every series name (L7a §3.5).
    legend: { show: false },
    axes: [
      {
        stroke: '#a1a1aa',
        grid: { stroke: '#27272a' },
        ticks: { stroke: '#3f3f46' },
        font: '10px sans-serif',
      },
      {
        stroke: '#a1a1aa',
        grid: { stroke: '#27272a' },
        ticks: { stroke: '#3f3f46' },
        font: '10px sans-serif',
        // Room for two decimals ("10.25"): the default axis width clipped the
        // last digit on g magnitudes ≥ 1 (L7a §3.5).
        size: 48,
        values: (_u: uPlot, splits: number[]) => splits.map((v) => v.toFixed(2)),
      },
    ],
    series: [
      {},
      ...ids.map((id) => ({
        label: roles.get(id) ?? id,
        stroke: roleHex(roles.get(id) ?? ''),
        width: 1.5,
      })),
    ],
  };
}

/**
 * Live acc-magnitude chart (uPlot), last 10 s, one series per phone.
 *
 * It subscribes IMPERATIVELY to the isolated `live` store — 10 Hz samples
 * update the plot via setData and never re-render React anywhere (SPEC §5.2,
 * proven by the render-count test in store.test.tsx).
 */
export function LiveChart() {
  const app = useApp();
  const phones = useSnapshot((s) => s.snapshot?.phones ?? EMPTY_PHONES);
  const rolesRef = useRef(new Map<string, string>());
  for (const p of phones) rolesRef.current.set(p.device_id, p.role);
  const seriesKey = phones
    .map((p) => p.device_id)
    .sort()
    .join(',');

  const hostRef = useRef<HTMLDivElement | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || seriesKey === '') return;
    // uPlot draws asynchronously (rAF); with a null 2D context (jsdom) it
    // would throw OUTSIDE this effect. Probe first and degrade gracefully.
    if (!document.createElement('canvas').getContext('2d')) {
      setUnavailable(true);
      return;
    }
    const ids = seriesKey.split(',');
    let plot: uPlot | null = null;
    try {
      plot = new uPlot(
        chartOptions(
          host.clientWidth,
          host.clientHeight,
          ids,
          rolesRef.current,
        ),
        { length: 0 } as unknown as uPlot.AlignedData,
        host,
      );
      setUnavailable(false);
    } catch {
      // Headless/jsdom without a 2D canvas: skip the chart, keep the page alive.
      setUnavailable(true);
      return;
    }

    const times: number[] = [];
    const series: number[][] = ids.map(() => []);
    const unsubscribe = app.live.subscribe(() => {
      const data = app.live.getState();
      if (!data || !plot) return;
      const t = data.t_ms / 1000;
      times.push(t);
      ids.forEach((id, i) => {
        const sample = data.phones[id];
        (series[i] as number[]).push(sample ? magnitude(sample.acc) : NaN);
      });
      const cutoff = t - WINDOW_S;
      while (times.length > 0 && (times[0] as number) < cutoff) {
        times.shift();
        series.forEach((s) => s.shift());
      }
      plot.setData([times, ...series] as uPlot.AlignedData);
    });

    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => {
        if (plot && host.clientWidth > 0) {
          plot.setSize({ width: host.clientWidth, height: Math.max(host.clientHeight, 80) });
        }
      });
      observer.observe(host);
    }

    return () => {
      unsubscribe();
      observer?.disconnect();
      plot?.destroy();
    };
  }, [app, seriesKey]);

  return (
    <div className="flex h-full min-h-24 w-full flex-col" data-testid="live-chart">
      <div className="mb-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5" data-testid="live-chart-legend">
        {phones.map((p) => (
          <span key={p.device_id} className="inline-flex items-center gap-1 text-[10px] font-bold text-zinc-400">
            <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: roleHex(p.role) }} />
            {p.role}
          </span>
        ))}
      </div>
      {unavailable ? (
        <div className="flex min-h-0 flex-1 items-center justify-center text-xs text-zinc-600">
          chart unavailable
        </div>
      ) : (
        <div ref={hostRef} className="min-h-0 flex-1" />
      )}
    </div>
  );
}

/** The chart box with the persisted "chart visible" view preference. */
export function LiveChartBox() {
  const [visible, setVisible] = useState(() => loadBoolPref('chartVisible', true));
  const toggle = () => {
    const next = !visible;
    setVisible(next);
    savePref('chartVisible', next ? '1' : '0');
  };
  return (
    <section className="flex h-full min-h-0 flex-col rounded-lg border border-zinc-800 bg-zinc-900/60 p-2">
      <div className="mb-1 flex items-center justify-between">
        <h2 className="text-xs font-black uppercase tracking-wider text-zinc-500">
          Acc magnitude · last {WINDOW_S} s
        </h2>
        <button
          type="button"
          onClick={toggle}
          className="rounded border border-zinc-700 px-2 py-0.5 text-[11px] font-bold text-zinc-400 hover:bg-zinc-800"
          aria-pressed={visible}
        >
          Chart: {visible ? 'on' : 'off'}
        </button>
      </div>
      <div className="min-h-0 flex-1">{visible && <LiveChart />}</div>
    </section>
  );
}
