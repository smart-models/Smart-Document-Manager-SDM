import assert from "node:assert/strict";
import test from "node:test";
import { partitionRagUnits } from "../dist/assets/rag-partition.js";

const compare = (a, b) => {
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  return 0;
};
function score(weights, groups, min, max) {
  const lower = BigInt(min), upper = BigInt(max);
  return groups.reduce((result, [start, end]) => {
    const sum = weights.slice(start, end).reduce((a, b) => a + BigInt(b), 0n);
    const delta = 2n * sum - lower - upper;
    return [result[0] + (sum < lower || sum > upper ? 1n : 0n),
      result[1] + (sum < lower ? lower - sum : 0n), result[2] + 1n,
      result[3] + (delta < 0n ? -delta : delta)];
  }, [0n, 0n, 0n, 0n]);
}
// Independent exhaustive oracle enumerates every legal cut, no queue or DP.
function exhaustive(weights, min, max) {
  let best = null, bestScore = null;
  function visit(start, groups) {
    if (start === weights.length) {
      const candidate = score(weights, groups, min, max);
      if (best === null || compare(candidate, bestScore) < 0) { best = groups; bestScore = candidate; }
      return;
    }
    let sum = 0n;
    for (let end = start + 1; end <= weights.length; end++) {
      sum += BigInt(weights[end - 1]);
      if (end > start + 1 && sum > BigInt(max)) break;
      visit(end, [...groups, [start, end]]);
    }
  }
  visit(0, []);
  return best;
}

test("linear optimizer matches all partitions, including stable ties, on 2500 seeded cases", () => {
  let seed = 928;
  const next = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed; };
  for (let sample = 0; sample < 2500; sample++) {
    const min = 1 + next() % 20, max = min + next() % 25;
    const weights = Array.from({ length: next() % 9 }, () => next() % 45);
    assert.deepEqual(partitionRagUnits(weights, min, max), exhaustive(weights, min, max),
      JSON.stringify({ weights, min, max }));
  }
});

test("large counts use exact arithmetic for sums, comparisons and scores", () => {
  const safe = Number.MAX_SAFE_INTEGER;
  for (const [weights, min, max] of [
    [[safe - 2, 1, 1, safe - 2, 0], safe - 1, safe],
    [[safe, 0, safe, 1], 1, safe],
    [[safe, safe, safe], safe, safe],
    [[1, 1, 0, 2, 0], safe - 1, safe],
  ]) assert.deepEqual(partitionRagUnits(weights, min, max), exhaustive(weights, min, max));
});

test("there is no arbitrary lookahead limit, including long zero runs", () => {
  assert.deepEqual(partitionRagUnits(Array(1200).fill(1), 800, 1500), [[0, 1200]]);
  const zeros = Array(20000).fill(0);
  assert.deepEqual(partitionRagUnits(zeros, 250, 400), [[0, zeros.length]]);
  assert.deepEqual(partitionRagUnits([100, ...zeros, 200], 250, 400), [[0, zeros.length + 2]]);
});
