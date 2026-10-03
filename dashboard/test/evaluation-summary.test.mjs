import assert from "node:assert/strict";
import test from "node:test";
import {renderEvaluationSummary} from "../evaluation-summary.js";

test("shadow evaluation keeps missing outcomes separate, escapes labels and never claims an edge", () => {
  const html = renderEvaluationSummary({mode:"shadow", counts:{primary_signals:5,primary_controls:10},
    horizons:{"24h<script>":{signal_outcome_status_counts:{eligible:1,pending:2,missing:1,late:1},complete_price_pairs:0}}});
  assert.match(html,/not a proven trading edge/);
  assert.match(html,/24h&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/<td>1<\/td><td>2<\/td><td>2<\/td><td>0<\/td>/);
  assert.match(renderEvaluationSummary(null), /Prospective observations pending/);
  assert.match(renderEvaluationSummary(null), /class="section-title-row"/);
  assert.match(html, /class="evaluation-note"/);
  assert.doesNotMatch(renderEvaluationSummary(null), /frozen signals|<td>0/);
});
