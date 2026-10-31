// Reproduce with: node frontend/benchmarks/rag-aggregation.mjs
import assert from "node:assert/strict";
import os from "node:os";
import { performance } from "node:perf_hooks";
import { partitionRagUnits } from "../dist/assets/rag-partition.js";
import { planRagAggregation } from "../dist/assets/rag-preflight.js";

const options = { min_tokens: 800, max_tokens: 1500,
  keep_tables_together: true, keep_code_blocks_together: true, page_boundary: false };
const median = values => values.toSorted((a, b) => a - b)[Math.floor(values.length / 2)];
function measure(run) {
  run(); // Warm-up is not included.
  const durations = [];
  for (let repeat = 0; repeat < 5; repeat++) {
    const start = performance.now();
    run();
    durations.push(performance.now() - start);
  }
  return Math.round(median(durations) * 100) / 100;
}
const results = [];
for (const size of [1000, 10000, 100000]) {
  for (const family of ["tiny", "mixed", "zeros"]) {
    let seed = 928;
    const weights = Array.from({ length: size }, () => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      return family === "tiny" ? 1 : family === "zeros" ? 0 : 20 + seed % 180;
    });
    const source = { chunks: weights.map((token_count, index) => ({
      id: index + 1, text: "Raw OCR paragraph.", page: 1, document: "benchmark.pdf", token_count,
    })) };
    const plan = planRagAggregation(source, options);
    assert.equal(plan.groups.reduce((sum, group) => sum + group.source_indices.length, 0), size);
    if (family === "tiny") assert.equal(plan.counts.within, plan.groups.length);
    results.push({ size, family, counts: plan.counts,
      optimizer_median_ms: measure(() => partitionRagUnits(weights, options.min_tokens, options.max_tokens)),
      preflight_median_ms: measure(() => planRagAggregation(source, options)) });
  }
}
process.stdout.write(JSON.stringify({ node: process.version, platform: process.platform,
  cpu: os.cpus()[0]?.model, repeats: 5, results }, null, 2) + "\n");
