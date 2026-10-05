import {guardR2Env} from "./r2-budget.js";

export async function archiveBackupResponse(env,request) {
  const supplied = request.headers.get("x-radar-archive-backup-secret");
  if (!env.RADAR_ARCHIVE_BACKUP_SECRET || supplied !== env.RADAR_ARCHIVE_BACKUP_SECRET) {
    return Response.json({ok:false,error:"archive_backup_authorization_required"},{status:403});
  }
  if (request.method !== "GET") return Response.json({ok:false,error:"GET required"},{status:405});
  const url = new URL(request.url), store = guardR2Env(env).RADAR_ARCHIVE;
  const key = url.searchParams.get("key");
  if (key) {
    if (!/^(history|runtime|outbox)\/[a-zA-Z0-9/_:.-]{1,1000}$/.test(key) || key.includes("..")) {
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
