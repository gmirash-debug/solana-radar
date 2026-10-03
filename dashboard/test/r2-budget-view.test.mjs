import assert from "node:assert/strict";
import test from "node:test";
import {r2BudgetView} from "../r2-budget-view.js";

const active = {enabled:true,initialized:true,status:"active",paused:false,usage:{class_a:10000,class_b:100000,storage_bytes:100000000},used_pct:{class_a:1,class_b:1,storage_bytes:1},resets_at:"2026-11-01T00:00:00.000Z"};
test("three quotas are visible; no alarm below the warning", () => {
  const view = r2BudgetView(active);
  assert.equal(view.alert, "");
  assert.equal((view.panel.match(/<meter /g) || []).length, 3);
  assert.match(view.panel,/0.100 \/ 10 GB/);
  assert.match(view.panel,/stored bytes do not reset/);
  assert.match(view.panel,/not a bill/);
});
test("80% warning and paused warning stay visible outside diagnostics", () => {
  assert.match(r2BudgetView({...active,used_pct:{class_a:80}}).alert,/approaching/);
  const view = r2BudgetView({...active,paused:true,status:"paused"});
  assert.match(view.alert,/2026-11-01 at 00:00 UTC/);
  assert.match(view.alert,/Storage must also remain below 9 GB/);
  assert.match(view.alert,/Current scans continue/);
});
test("unavailable does not pretend usage is zero", () => {
  const view = r2BudgetView({enabled:true,status:"unavailable"});
  assert.match(view.alert,/blocked/);
  assert.match(view.panel,/not verified/);
  assert.doesNotMatch(view.panel,/0\.00%/);
});
test("no untrusted HTML or NaN is rendered", () => {
  const view = r2BudgetView({...active,usage:{class_a:"<script>"},resets_at:'<img onerror="evil()">',paused:true});
  assert.doesNotMatch(view.panel,/<script|<img|NaN/);
});
test("disabled or missing guards do not fabricate a budget", () => {
  assert.deepEqual(r2BudgetView(null),{alert:"",panel:""});
  assert.deepEqual(r2BudgetView({enabled:false}),{alert:"",panel:""});
});
