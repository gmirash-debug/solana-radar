import test from "node:test";
import assert from "node:assert/strict";
import {publishedDetailPath, loadTokenDetail} from "../static-detail.js";

const generation = "2026-10-02T22:00:00Z";
const path = `data/token-details/${"a".repeat(64)}.json`;
const manifest = {generation, files:{mint:path}};
const detail = {ok:true, token_key:"mint", report_source_updated_at:generation};
const response = (body, ok=true) => ({ok, status:ok?200:500, json:async()=>body});
const args = {manifest, generation, key:"mint", baseUrl:"https://worker.test", accepts:body=>body.token_key==="mint" && body.report_source_updated_at===generation};

test("static paths cannot redirect to another host or escape their directory", () => {
  assert.equal(publishedDetailPath(manifest, "mint", generation), path);
  assert.equal(publishedDetailPath(manifest, "mint", "old"), null);
  for (const invalid of ["https://evil.test/file", "//evil.test/file", "../secret", `${path}?x=1`]) {
    assert.equal(publishedDetailPath({generation, files:{mint:invalid}}, "mint", generation), null);
  }
});
test("matching published wallets load without consuming a database read", async () => {
  const calls=[];
  assert.equal(await loadTokenDetail({...args, fetcher:async url=>{calls.push(url); return response(detail);}}), detail);
  assert.deepEqual(calls, [path]);
});
test("stale or broken static detail falls back without applying it", async () => {
  for (const bad of [response({...detail, report_source_updated_at:"old"}), response({error:"quota"}, false)]) {
    const calls=[];
    const result=await loadTokenDetail({...args, fetcher:async url=>{calls.push(url); return calls.length===1?bad:response(detail);}});
    assert.equal(result, detail); assert.equal(calls.length, 2);
  }
});
test("both missing sources preserve the summary and report an error", async () => {
  await assert.rejects(loadTokenDetail({...args, fetcher:async()=>response({error:"unavailable"}, false)}), /unavailable/);
});
