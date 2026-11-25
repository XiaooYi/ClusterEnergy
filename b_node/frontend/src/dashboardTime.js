import { powerSeries } from "./powerSeries.js";

const beijingClock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  hourCycle: "h23",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

export function formatBeijingClock(value) {
  if (value == null || !Number.isFinite(new Date(value).getTime())) return "--:--:--";
  return beijingClock.format(new Date(value));
}

export function beijingDate(timestamp) {
  return new Date(timestamp + 8 * 3_600_000).toISOString().slice(0, 10);
}

export function dayPowerSeries(date, rows) {
  const start = Date.parse(`${date}T00:00:00+08:00`);
  const end = start + 86_400_000;
  const samples = rows.filter((row) => row.ts.startsWith(`${date}T`))
    .map((row) => ({ ...row, timestamp: `${row.ts}+08:00` }));

  return {
    start,
    end,
    average: powerSeries(samples, "timestamp", "avg_power", 60_000),
    peak: powerSeries(samples, "timestamp", "max_power", 60_000),
  };
}
