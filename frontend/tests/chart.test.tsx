import { render } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import EChartBox from "../src/components/EChartBox";

const chart = vi.hoisted(() => ({resize: vi.fn(), dispose: vi.fn(), setOption: vi.fn()}));
vi.mock("echarts/core", () => ({use: vi.fn(), init: vi.fn(() => chart)}));

it("updates ECharts, observes its container and releases resources on unmount", () => {
  const observe = vi.fn();
  const disconnect = vi.fn();
  let notifyResize = () => {};
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { notifyResize = callback; }
    observe = observe;
    disconnect = disconnect;
  });
  const view = render(<EChartBox label="净值" option={{animation: false}} />);
  expect(observe).toHaveBeenCalledWith(view.getByRole("img",{name:"净值"}));
  notifyResize();
  expect(chart.resize).toHaveBeenCalledTimes(1);
  view.rerender(<EChartBox label="净值" option={{animation: true}} />);
  expect(chart.setOption).toHaveBeenLastCalledWith({animation: true}, {notMerge: true});
  view.unmount();
  expect(disconnect).toHaveBeenCalledTimes(1);
  expect(chart.dispose).toHaveBeenCalledTimes(1);
});
