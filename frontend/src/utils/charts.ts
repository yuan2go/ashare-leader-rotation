import type { ChartOption } from "../components/EChartBox";

export const chartFrame = {
  animation: false,
  textStyle: {fontFamily: "Cascadia Mono, Consolas, monospace", color: "#a2b2c6", fontSize: 11},
  grid: {left: 13, right: 25, top: 20, bottom: 15, containLabel: true},
  tooltip: {
    trigger: "axis", renderMode: "richText",
    backgroundColor: "#151f2b", borderColor: "#314255",
    textStyle: {color: "#f2f6fa", fontSize: 12},
  },
} satisfies ChartOption;

export const categoryAxis = {
  type: "category",
  axisLine: {lineStyle: {color: "#2d3a49"}},
  axisLabel: {color: "#a2b2c6"},
  axisTick: {show: false},
} as const;

export const valueAxis = {
  type: "value",
  splitLine: {lineStyle: {color: "#263340", type: "dashed"}},
} as const;
