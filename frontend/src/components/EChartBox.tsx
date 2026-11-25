import { BarChart, LineChart } from "echarts/charts";
import type { BarSeriesOption, LineSeriesOption } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import type { GridComponentOption, TooltipComponentOption } from "echarts/components";
import { init, use } from "echarts/core";
import type { ComposeOption, EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

use([BarChart, LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

export type ChartOption = ComposeOption<BarSeriesOption | LineSeriesOption | GridComponentOption | TooltipComponentOption>;

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
