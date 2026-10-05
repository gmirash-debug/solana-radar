import {guardR2Env,withR2BudgetBatch} from "./r2-budget.js";

const validKey = key => typeof key==="string" && /^(history|runtime|outbox)\/[a-zA-Z0-9/_:.-]{1,1000}$/.test(key) && !key.includes("..");

async function batchResponse(env,request) {
  const reader=request.body?.getReader();
  if (!reader) throw new Error("archive_backup_body_required");
  const chunks=[];let size=0;
  try {
    while(true) {const item=await reader.read();if(item.done)break;size+=item.value.byteLength;
      if(size>32768)throw new Error("archive_backup_body_too_large");chunks.push(item.value);}
  } finally {await reader.cancel().catch(()=>{});}
  const raw=new Uint8Array(size);let offset=0;for(const chunk of chunks){raw.set(chunk,offset);offset+=chunk.byteLength;}
  const keys=JSON.parse(new TextDecoder().decode(raw)).keys;
  if (!Array.isArray(keys) || !keys.length || keys.length>16 || new Set(keys).size!==keys.length || keys.some(key=>!validKey(key))) {
    return Response.json({ok:false,error:"archive_backup_batch_invalid"},{status:400});
  }
  return withR2BudgetBatch(env,keys.map(key=>({kind:"get",key})),async scoped=>{
    const frames=[];let bytes=0;
    for(const key of keys) {
      const object=await scoped.RADAR_ARCHIVE.get(key);
      if(!object || !Number.isSafeInteger(object.size) || object.size<0 || bytes+object.size>8*1024*1024) {
        throw new Error("archive_backup_batch_capacity_or_missing");
      }
      const body=new Uint8Array(await object.arrayBuffer());
      if(body.byteLength!==object.size)throw new Error("archive_backup_body_incomplete");
      bytes+=body.byteLength;
      const header=new TextEncoder().encode(JSON.stringify({key,bytes:object.size,etag:object.etag}));
      const length=new Uint8Array(4);new DataView(length.buffer).setUint32(0,header.byteLength);
      frames.push(length,header,body);
    }
    return new Response(new Blob(frames).stream(),{headers:{"content-type":"application/octet-stream",
      "content-encoding":"identity","x-radar-batch-count":String(keys.length)}});
  });
}

export async function archiveBackupResponse(env,request) {
  const supplied = request.headers.get("x-radar-archive-backup-secret");
  if (!env.RADAR_ARCHIVE_BACKUP_SECRET || supplied !== env.RADAR_ARCHIVE_BACKUP_SECRET) {
    return Response.json({ok:false,error:"archive_backup_authorization_required"},{status:403});
  }
  if (new URL(request.url).pathname.endsWith("/batch") && request.method==="POST") return batchResponse(env,request);
  if (request.method !== "GET") return Response.json({ok:false,error:"GET required"},{status:405});
  const url = new URL(request.url), store = guardR2Env(env).RADAR_ARCHIVE;
  const key = url.searchParams.get("key");
  if (key) {
    if (!validKey(key)) {
      return Response.json({ok:false,error:"archive_backup_key_invalid"},{status:400});
    }
    const object = await store.get(key);
    if (!object) return Response.json({ok:false,error:"archive_backup_missing"},{status:404});
    return new Response(object.body,{headers:{"content-type":"application/octet-stream",
      "content-length":String(object.size),"x-radar-source-etag":object.etag}});
  }
  const cursor = url.searchParams.get("cursor");
  if (cursor && cursor.length>2048) return Response.json({ok:false,error:"archive_backup_cursor_invalid"},{status:400});
  const page = await store.list({limit:1000,...(cursor ? {cursor} : {})});
  return Response.json({ok:true,objects:page.objects.map(row=>({key:row.key,bytes:row.size,etag:row.etag})),
    cursor:page.truncated ? page.cursor : null});
}
