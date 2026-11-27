import { BarChart, CandlestickChart, HeatmapChart, LineChart, ScatterChart } from "echarts/charts";
import type { BarSeriesOption, CandlestickSeriesOption, HeatmapSeriesOption, LineSeriesOption, ScatterSeriesOption } from "echarts/charts";
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, VisualMapComponent } from "echarts/components";
import type { DataZoomComponentOption, GridComponentOption, LegendComponentOption, TooltipComponentOption, VisualMapComponentOption } from "echarts/components";
import { init, use } from "echarts/core";
import type { ComposeOption, EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

use([BarChart, CandlestickChart, HeatmapChart, LineChart, ScatterChart, DataZoomComponent, GridComponent,
  LegendComponent, TooltipComponent, VisualMapComponent, CanvasRenderer]);

export type ChartOption = ComposeOption<BarSeriesOption | CandlestickSeriesOption | HeatmapSeriesOption |
  LineSeriesOption | ScatterSeriesOption | DataZoomComponentOption | GridComponentOption | LegendComponentOption |
  TooltipComponentOption | VisualMapComponentOption>;

export default function EChartBox({label, option, className = ""}: {
  label: string; option: ChartOption; className?: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<EChartsType | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const instance = init(host.current, undefined, {renderer: "canvas"});
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    chart.current?.setOption(option, {notMerge: true});
  }, [option]);

  return <div ref={host} className={`chart-box ${className}`} role="img" aria-label={label} />;
}
