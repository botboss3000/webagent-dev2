// RECORDER-HOOK: byte-bounded transport, shared by live flush and old outbox
// retries. Stay below the app's 1 MiB JSON boundary, including escaped Unicode.
// Sequence IDs never change when a stored chunk is split for delivery.
export const BATCH_BYTES=256*1024;
export const bytes=value=>new Blob([JSON.stringify(value)]).size;
export function batches(records, limit=BATCH_BYTES) {
  const result=[];let batch=[],size=2048;
  for(const row of records) {
    const n=bytes(row)+1;
    if(batch.length && (size+n>limit || batch.length>=200)) {
      result.push(batch);batch=[];size=2048;
    }
    batch.push(row);size+=n;
  }
  if(batch.length)result.push(batch);
  return result;
}

export function textParts(text, size=24000) {
  const parts=[];
  for(let start=0;start<text.length;) {
    let end=Math.min(start+size,text.length);
    const code=text.charCodeAt(end-1);
    if(end<text.length && code>=0xD800 && code<=0xDBFF)end--;
    parts.push(text.slice(start,end));start=end;
  }
  return parts;
}
