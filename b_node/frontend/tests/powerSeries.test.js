import assert from "node:assert/strict";
import test from "node:test";
import { powerSeries } from "../src/powerSeries.js";

test("preserves a sampled zero and breaks the line for missing samples", () => {
  const start = Date.parse("2026-11-25T11:50:00.000Z");
  const rows = [
    { collected_at: "2026-11-25T11:50:00Z", watts: 477.1 },
    { collected_at: "2026-11-25T11:50:05Z", watts: 0 },
    { collected_at: "2026-11-25T11:50:15Z", watts: 849 },
    { collected_at: "2026-11-25T11:50:20Z" },
  ];
  assert.deepEqual(powerSeries(rows, "collected_at", "watts", 5000), [
    [start, 477.1], [start + 5000, 0], [start + 10000, null],
    [start + 15000, 849], [start + 20000, null],
  ]);
});
