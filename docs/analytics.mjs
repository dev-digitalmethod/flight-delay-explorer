const chunks = new Map();

async function loadChunk(manifest, airport) {
  if (!chunks.has(airport)) {
    const name = manifest.chunks[airport];
    if (!name) return [];
    const request = (async () => {
      const response = await fetch(new URL("./data/" + name, import.meta.url));
      if (!response.ok) throw new Error("Could not load flight data. Try evaluating again.");
      const bytes = new Uint8Array(await response.arrayBuffer());
      if (bytes[0] === 0x1f && bytes[1] === 0x8b) {
        const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
        return new Response(stream).json();
      }
      return JSON.parse(new TextDecoder().decode(bytes));
    })();
    chunks.set(airport, request);
    if (chunks.size > 8) chunks.delete(chunks.keys().next().value);
    request.catch(() => {
      if (chunks.get(airport) === request) chunks.delete(airport);
    });
  }
  return chunks.get(airport);
}

function counter() {
  return {values: new Map(), missing: 0, outside: 0};
}

function collect(rows, req, byAirline) {
  const series = new Map();
  for (const [month, airline, outcome, missing, frequencies] of rows) {
    if (month < req.start || month > req.end ||
        (req.filters.airline && airline !== req.filters.airline) ||
        (req.outcome !== "all" && outcome !== req.outcome)) continue;
    const key = byAirline ? airline : "total";
    if (!series.has(key)) series.set(key, new Map());
    const months = series.get(key);
    if (!months.has(month)) months.set(month, counter());
    const target = months.get(month);
    target.missing += missing;
    for (let i = 0; i < frequencies.length; i += 2) {
      const value = frequencies[i];
      const count = frequencies[i + 1];
      if ((req.min !== null && value < req.min) || (req.max !== null && value > req.max)) {
        target.outside += count;
      } else {
        target.values.set(value, (target.values.get(value) || 0) + count);
      }
    }
  }
  return series;
}

function merge(months) {
  const total = counter();
  for (const item of months) {
    total.missing += item.missing;
    total.outside += item.outside;
    for (const [value, count] of item.values) {
      total.values.set(value, (total.values.get(value) || 0) + count);
    }
  }
  return total;
}

function weightedStats(item) {
  const frequencies = [...item.values].sort(([a], [b]) => a - b);
  let n = 0;
  let sum = 0;
  for (const [value, count] of frequencies) {
    n += count;
    sum += value * count;
  }
  const mean = n ? sum / n : null;
  let m2 = 0;
  for (const [value, count] of frequencies) m2 += count * (value - mean) ** 2;
  function atRank(rank) {
    let cursor = 0;
    for (const [value, count] of frequencies) {
      cursor += count;
      if (rank < cursor) return value;
    }
  }
  function quantile(p) {
    if (!n) return null;
    const position = (n - 1) * p;
    const lower = atRank(Math.floor(position));
    const upper = atRank(Math.ceil(position));
    return lower + (upper - lower) * (position % 1);
  }
  return {
    n, missing: item.missing, outside: item.outside, mean,
    median: quantile(0.5), p95: quantile(0.95), p01: quantile(0.01), p99: quantile(0.99),
    stddev: n > 1 ? Math.sqrt(m2 / (n - 1)) : null,
    min: n ? frequencies[0][0] : null, max: n ? frequencies.at(-1)[0] : null,
  };
}

function makeSeries(id, label, months, dates, aggregate) {
  const combined = merge(months.values());
  const periods = dates.map(month => months.has(month) ? weightedStats(months.get(month)) : null);
  return {
    id, label, summary: weightedStats(combined), frequencies: combined.values,
    time: periods.map(period => period?.[aggregate === "count" ? "n" : aggregate] ?? null),
    timeCounts: periods.map(period => period?.n ?? 0),
  };
}

function binEdges(left, right) {
  if (left === right) return [left - 0.5, right + 0.5];
  const width = (right - left) / 30;
  return Array.from({length: 31}, (_, i) => i === 30 ? right : left + width * i);
}

function histogram(frequencies, edges) {
  const counts = Array(edges.length - 1).fill(0);
  for (const [value, count] of frequencies) {
    if (value < edges[0] || value > edges.at(-1)) continue;
    const nextEdge = edges.findIndex(edge => edge > value);
    counts[nextEdge === -1 ? counts.length - 1 : nextEdge - 1] += count;
  }
  return counts;
}

function distributions(series) {
  const nonempty = series.filter(item => item.summary.n);
  if (!nonempty.length) return {edges: [], centralEdges: []};
  const edges = binEdges(Math.min(...nonempty.map(item => item.summary.min)),
    Math.max(...nonempty.map(item => item.summary.max)));
  const centralEdges = binEdges(Math.min(...nonempty.map(item => item.summary.p01)),
    Math.max(...nonempty.map(item => item.summary.p99)));
  for (const item of nonempty) {
    const frequencies = [...item.frequencies].sort(([a], [b]) => a - b);
    item.histogram = histogram(frequencies, edges);
    item.centralHistogram = histogram(frequencies, centralEdges);
    item.ecdfX = [edges[0]];
    item.ecdfY = [0];
    let cumulative = 0;
    for (const [value, count] of frequencies) {
      cumulative += count;
      item.ecdfX.push(value);
      item.ecdfY.push(cumulative / item.summary.n);
    }
    if (item.ecdfX.at(-1) < edges.at(-1)) {
      item.ecdfX.push(edges.at(-1));
      item.ecdfY.push(1);
    }
  }
  return {edges, centralEdges};
}

function selectedMonths(start, end) {
  const months = [];
  const cursor = new Date(start + "-01T00:00:00Z");
  let key = start;
  while (key <= end) {
    months.push(key);
    cursor.setUTCMonth(cursor.getUTCMonth() + 1);
    key = cursor.toISOString().slice(0, 7);
  }
  return months;
}

export async function evaluate(req, manifest, readChunk = loadChunk) {
  const monthPattern = /^\d{4}-(0[1-9]|1[0-2])$/;
  if (!monthPattern.test(req.start) || !monthPattern.test(req.end)) {
    throw new Error("Enter start and end months in YYYY-MM format.");
  }
  if (req.start < manifest.months[0] || req.end > manifest.months.at(-1)) {
    throw new Error("Choose months within the available BTS data.");
  }
  if (req.start > req.end) throw new Error("The end month must be on or after the start month.");
  if (!req.total && !req.group) throw new Error("Show Total or choose a comparison group.");
  if (req.min !== null && req.max !== null && req.min > req.max) {
    throw new Error("The maximum signal value must be at least the minimum.");
  }
  const dates = selectedMonths(req.start, req.end);
  const series = [];
  const origin = req.filters.origin || "*";

  if (req.total || req.group === "airline") {
    const rows = await readChunk(manifest, origin);
    if (req.total) {
      const months = collect(rows, req, false).get("total") || new Map();
      series.push(makeSeries("total", "Total", months, dates, req.aggregate));
    }
    if (req.group === "airline") {
      const byAirline = collect(rows, req, true);
      const groups = req.groups.length ? req.groups : [...byAirline.keys()].filter(Boolean)
        .map(code => [code, weightedStats(merge(byAirline.get(code).values())).n])
        .filter(([, count]) => count)
        .sort(([a, an], [b, bn]) => bn - an || a.localeCompare(b)).slice(0, 3).map(([code]) => code);
      for (const code of groups) {
        series.push(makeSeries("airline:" + code, code, byAirline.get(code) || new Map(),
          dates, req.aggregate));
      }
    }
  }
  if (req.group === "origin") {
    const groups = req.groups.length ? req.groups : (origin === "*" ? manifest.topOrigins : [origin]);
    series.push(...await Promise.all(groups.map(async code => {
      const rows = origin === "*" || origin === code ? await readChunk(manifest, code) : [];
      const months = collect(rows, req, false).get("total") || new Map();
      return makeSeries("origin:" + code, code, months, dates, req.aggregate);
    })));
  }
  const plots = distributions(series);
  return {
    label: "Departure delay", unit: "min", grain: "month", aggregate: req.aggregate,
    dates: dates.map(month => month + "-01"), missingMonths: dates.filter(month => !manifest.months.includes(month)),
    series: series.map(({frequencies, ...item}) => item), ...plots,
  };
}
