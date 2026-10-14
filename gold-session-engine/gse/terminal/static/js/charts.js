// Thin wrappers around TradingView Lightweight Charts (vendored, Apache-2.0).
import { tzOffsetSeconds } from "./lib.js";

const LWC = window.LightweightCharts;

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function baseOptions() {
  return {
    autoSize: true,
    layout: { background: { type: "solid", color: "transparent" }, textColor: css("--text-3"), fontSize: 11,
      fontFamily: css("--mono") },
    grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
    rightPriceScale: { borderColor: css("--line-2") },
    timeScale: { borderColor: css("--line-2"), timeVisible: true, secondsVisible: false },
    crosshair: { mode: LWC.CrosshairMode.Normal },
  };
}

const shift = (ts) => ts + tzOffsetSeconds(ts);

export class PriceChart {
  constructor(el) {
    this.el = el;
    this.chart = LWC.createChart(el, baseOptions());
    this.candles = this.chart.addCandlestickSeries({
      upColor: css("--up"), downColor: css("--down"), borderVisible: false,
      wickUpColor: css("--up"), wickDownColor: css("--down"),
    });
    this.ema = this.chart.addLineSeries({ color: css("--gold"), lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    this.vwap = this.chart.addLineSeries({ color: css("--info"), lineWidth: 1, lineStyle: 0, priceLineVisible: false, lastValueVisible: false });
    this.lines = [];
    this.fitted = false;
    this.lastKey = "";
  }

  update(data) {
    if (!data || !data.bars) return;
    const bars = data.bars.filter((b) => b.t);
    const key = bars.length ? `${bars[0].t}-${bars.length}` : "";
    const candles = bars.map((b) => ({ time: shift(b.t), open: b.o, high: b.h, low: b.l, close: b.c }));
    this.candles.setData(candles);
    this.ema.setData(bars.filter((b) => b.ema).map((b) => ({ time: shift(b.t), value: b.ema })));
    // the VWAP restarts every day at 07:00 London: draw only the current session's line
    let lastGap = -1;
    bars.forEach((b, i) => { if (!b.vwap) lastGap = i; });
    this.vwap.setData(bars.slice(lastGap + 1).map((b) => ({ time: shift(b.t), value: b.vwap })));

    for (const l of this.lines) this.candles.removePriceLine(l);
    this.lines = [];
    const add = (price, color, title, style = 2, width = 1) => {
      if (price === null || price === undefined) return;
      this.lines.push(this.candles.createPriceLine({ price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title }));
    };
    if (data.asian) {
      add(data.asian.high, css("--text-2"), "Asian high", 2);
      add(data.asian.low, css("--text-2"), "Asian low", 2);
    }
    if (bars.length) {
      const last = bars[bars.length - 1].c;
      const step = data.level_step || 20;
      const base = Math.round(last / step) * step;
      for (const lvl of [base - step, base, base + step]) add(lvl, css("--line-2"), `$${lvl}`, 1);
    }
    for (const p of data.positions || []) {
      const c = p.side > 0 ? css("--up") : css("--down");
      add(p.entry, c, `${p.module} entry`, 0, 2);
      add(p.sl, css("--down"), `${p.module} SL`, 2);
      add(p.tp, css("--up"), `${p.module} TP`, 2);
    }
    for (const o of data.orders || []) add(o.entry, css("--warn"), `${o.module} ${o.side > 0 ? "buy" : "sell"} stop`, 3);

    if (key !== this.lastKey && candles.length) {
      this.lastKey = key;
      // show the last ~140 bars; widen again if the view started out with only a few bars
      const r = this.chart.timeScale().getVisibleLogicalRange();
      const want = Math.min(140, candles.length);
      if (!this.fitted || !r || r.to - r.from < Math.min(60, want - 1)) {
        this.chart.timeScale().setVisibleLogicalRange({ from: candles.length - want, to: candles.length + 5 });
        this.fitted = true;
      }
    }
  }

  destroy() { this.chart.remove(); }
}

export class LineChart {
  constructor(el, { percent = false } = {}) {
    this.chart = LWC.createChart(el, { ...baseOptions(), timeScale: { ...baseOptions().timeScale, timeVisible: false } });
    this.series = [];
    this.percent = percent;
  }

  set(seriesDefs) {
    for (const s of this.series) this.chart.removeSeries(s);
    this.series = [];
    for (const d of seriesDefs) {
      const s = d.type === "area"
        ? this.chart.addAreaSeries({ lineColor: d.color, topColor: d.fill || "transparent", bottomColor: "transparent", lineWidth: 2, priceLineVisible: false })
        : d.type === "hist"
          ? this.chart.addHistogramSeries({ color: d.color, priceLineVisible: false })
          : this.chart.addLineSeries({ color: d.color, lineWidth: d.width || 2, lineStyle: d.style || 0, priceLineVisible: false, lastValueVisible: d.lastValue !== false });
      s.setData(d.data);
      this.series.push(s);
    }
    this.chart.timeScale().fitContent();
  }

  destroy() { this.chart.remove(); }
}

export function dayTime(iso) {
  // "2024-03-05" -> business-day object for daily series
  const [y, m, d] = iso.split("-").map(Number);
  return { year: y, month: m, day: d };
}

export { css };
