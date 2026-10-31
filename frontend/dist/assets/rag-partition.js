// Exact ordered partition. See docs/research/rag-aggregation.md for the proof.
// Priority: out-of-range count, missing tokens below minimum, group count,
// then total distance from midpoint. Only indivisible units may exceed maximum.
export function partitionRagUnits(weights, minimum, maximum) {
  const n = weights.length;
  if (!n) return [];
  const total = weights.reduce((sum, weight) => sum + weight, 0);
  // Scores and transformed queue keys must remain exact, not just input counts.
  const wide = !Number.isSafeInteger(4 * total + n * (minimum + maximum));
  const cast = wide ? BigInt : Number;
  const zero = cast(0), two = cast(2), lower = cast(minimum), upper = cast(maximum);
  const center = lower + upper, middle = wide ? center / two : Math.floor(center / 2);
  const prefix = [zero];
  for (let i = n - 1; i >= 0; i--) prefix.push(prefix.at(-1) + cast(weights[i]));
  const best = [[0, zero, 0, zero]], previous = [0];
  const windows = [
    { min: zero, max: lower - cast(1), below: true, sign: two },
    { min: lower, max: middle, below: false, sign: two },
    { min: middle + cast(1), max: upper, below: false, sign: -two },
  ].filter(window => window.min <= window.max)
    .map(window => ({ ...window, queue: [], head: 0, next: 0 }));
  const compare = (a, b) => {
    for (let i = 0; i < 4; i++) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
    return 0;
  };
  for (let end = 1; end <= n; end++) {
    const sum = prefix[end];
    let chosen = null, split = -1;
    const consider = (score, index) => {
      const order = chosen === null ? -1 : compare(score, chosen);
      // Stable ties prefer the shorter first group in original source order.
      if (order < 0 || (order === 0 && index > split)) { chosen = score; split = index; }
    };
    for (const window of windows) {
      const { queue } = window, low = sum - window.max, high = sum - window.min;
      while (window.next < end && prefix[window.next] <= high) {
        const index = window.next++, score = best[index];
        const key = [score[0], score[1] + (window.below ? prefix[index] : zero),
          score[2], score[3] + window.sign * prefix[index]];
        while (queue.length > window.head && compare(key, queue.at(-1).key) <= 0) queue.pop();
        queue.push({ index, key });
      }
      while (window.head < queue.length && prefix[queue[window.head].index] < low) window.head++;
      const entry = queue[window.head];
      if (entry) {
        const key = entry.key;
        consider([key[0] + (window.below ? 1 : 0),
          key[1] + (window.below ? lower - sum : zero), key[2] + 1,
          key[3] + (window.sign === two ? center - two * sum : two * sum - center)], entry.index);
      }
    }
    const weight = prefix[end] - prefix[end - 1];
    if (weight > upper) {
      const score = best[end - 1];
      consider([score[0] + 1, score[1], score[2] + 1, score[3] + two * weight - center], end - 1);
    }
    best.push(chosen); previous.push(split);
  }
  const groups = [];
  for (let end = n; end > 0; end = previous[end]) groups.push([n - end, n - previous[end]]);
  return groups;
}
