import assert from "node:assert/strict";
import test from "node:test";
import { autotuneTiles, validateHealthResponse, validateStatsResponse, validateMetricsResponse } from "../dist/assets/models.js";

test("monitor validates successful response bodies before showing them", () => {
  assert.equal(validateHealthResponse({ status: "healthy", vllm: "reachable", model: "test" }), null);
  for (const value of [null, [], {}, { status: "healthy", vllm: 42, model: "test" }]) {
    assert.ok(validateHealthResponse(value));
  }
  assert.equal(validateStatsResponse({ active: 0, queue_size: 0 }), null);
  for (const value of [null, [], {}, { active: -1, queue_size: 0 }, { active: {}, queue_size: 0 }, { active: 0, queue_size: Infinity }]) {
    assert.ok(validateStatsResponse(value));
  }
  for (const value of ["", "# HELP only comments", "<html>Error</html>", "ocr_requests_total -1", "ocr_requests_total NaN", "ocr_requests_total 12bad", "ocr_requests_total 0xA", "ocr_requests_total +Inf", 'ocr_requests_total{endpoint=oops} 10', 'ocr_requests_total{endpoint="a",endpoint="b"} 10']) {
    assert.ok(validateMetricsResponse(value), value);
  }
  assert.equal(validateMetricsResponse("process_start_time_seconds 100\nocr_active_requests 0\n"), null);
  assert.equal(validateMetricsResponse('ocr_requests_total{endpoint="parse",status="200"} 10\n'), null);
  assert.equal(validateMetricsResponse('ocr_requests_total{endpoint="parse",status="200",} 1e1 12345\n'), null);
  assert.ok(validateMetricsResponse("ocr_unknown_metric 1\n"));
});

test("autotune shows overrides separately from profile decisions", () => {
  const html = autotuneTiles({ decisions: { vl_lanes: 8 }, effective: { vl_lanes: 2, layout_threads: null } });
  assert.match(html, /vl lanes \(profile\)/);
  assert.match(html, /vl lanes \(in effect\)/);
  assert.match(html, /layout threads \(in effect\)/);
  assert.match(html, /default/);
  const equal = autotuneTiles({ decisions: { vl_lanes: 2 }, effective: { vl_lanes: 2 } });
  assert.doesNotMatch(equal, /\(profile\)|\(in effect\)/);
  assert.match(autotuneTiles({ effective: { vl_lanes: 2 } }), /\(in effect\)/);
});
