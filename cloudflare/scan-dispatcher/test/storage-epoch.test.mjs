import assert from "node:assert/strict";
import {test} from "node:test";
import worker from "../src/index.js";

const epoch="20261007-clean-v1";
const request=(method,headers={})=>new Request("https://radar.example/api/not-real",{method,headers});

test("a missing or stale generation cannot write to the reset storage",async()=>{
  for (const headers of [{},{"x-radar-storage-epoch":"old-generation"}]) {
    const reply=await worker.fetch(request("POST",headers),{STORAGE_EPOCH:epoch});
    assert.equal(reply.status,409);
    assert.equal((await reply.json()).error,"storage_epoch_mismatch");
  }
});

test("generation fencing does not replace authentication",async()=>{
  const reply=await worker.fetch(request("POST",{"x-radar-storage-epoch":epoch}),
    {STORAGE_EPOCH:epoch,RADAR_INGEST_SECRET:"required"});
  assert.equal(reply.status,401);
});

test("reads and frozen-write protection remain independent of generation",async()=>{
  const read=await worker.fetch(request("GET"),{STORAGE_EPOCH:epoch});
  assert.notEqual(read.status,409);
  const frozen=await worker.fetch(request("POST",{"x-radar-storage-epoch":epoch}),
    {STORAGE_EPOCH:epoch,STORAGE_WRITES_FROZEN:"true"});
  assert.equal(frozen.status,503);
  assert.equal((await frozen.json()).error,"storage_cutover_writes_frozen");
});
