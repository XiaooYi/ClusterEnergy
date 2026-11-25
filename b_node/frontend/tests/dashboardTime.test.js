import assert from "node:assert/strict";
import test from "node:test";
import { dayPowerSeries, formatBeijingClock } from "../src/dashboardTime.js";

test("formats UTC samples using Beijing time across midnight", () => {
  assert.equal(formatBeijingClock("2026-11-25T16:02:30Z"), "00:02:30");
});

test("keeps an entire Beijing day on the axis and leaves unsampled minutes blank", () => {
  const result = dayPowerSeries("2026-11-25", [
    { ts: "2026-11-25T19:12", avg_power: 250, max_power: 400 },
    { ts: "2026-11-25T19:14", avg_power: 0, max_power: 0 },
  ]);
  const start = Date.parse("2026-11-24T16:00:00Z");
  const reading = Date.parse("2026-11-25T11:12:00Z");
  assert.equal(result.start, start);
  assert.equal(result.end, start + 86_400_000);
  assert.deepEqual(result.average, [[reading, 250], [reading + 60_000, null], [reading + 120_000, 0]]);
  assert.deepEqual(result.peak, [[reading, 400], [reading + 60_000, null], [reading + 120_000, 0]]);
});
