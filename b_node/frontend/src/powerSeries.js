export function powerSeries(rows, timestampField, wattsField, intervalMs) {
  const points = [];
  let previousTime = null;

  for (const row of rows) {
    const time = Date.parse(row[timestampField]);
    if (!Number.isFinite(time)) continue;
    if (previousTime !== null && time - previousTime > intervalMs * 1.5) {
      points.push([previousTime + intervalMs, null]);
    }
    const watts = row[wattsField];
    points.push([time, Number.isFinite(watts) ? watts : null]);
    previousTime = time;
  }

  return points;
}
