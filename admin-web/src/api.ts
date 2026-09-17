export class APIError extends Error { constructor(public readonly status:number,message:string){super(message)} }
export async function api<T=any>(path:string,method='GET',body?:unknown):Promise<T>{
 const csrf=document.cookie.split('; ').find(s=>s.startsWith('__Host-homeai-csrf='))?.split('=').slice(1).join('=')??'';
 const r=await fetch('/api/v1'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(csrf)},body:body===undefined?undefined:JSON.stringify(body)});
 const value=await r.json(); if(!r.ok)throw new APIError(r.status,typeof value.detail==='string'?value.detail:'操作未完成'); return value;
}

export async function uploadDocument(file:File):Promise<{id:string}>{
 const csrf=document.cookie.split('; ').find(s=>s.startsWith('__Host-homeai-csrf='))?.split('=').slice(1).join('=')??'';
 const form=new FormData();form.append('file',file);
 const response=await fetch('/api/v1/files',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':decodeURIComponent(csrf)},body:form});
 const value=await response.json();
 if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:'上传失败');
 return value;
}


export async function loadAuthorizedData():Promise<{records:any[]}>{
 for(let attempt=0;attempt<2;attempt++){
  try{
   const snapshot=await api('/sync/snapshot','POST');
   const records=new Map<string,any>();let offset=0;
   while(true){
    const page=await api('/sync/snapshot/'+snapshot.snapshot_id+'?offset='+offset+'&limit=100');
    for(const record of page.records)records.set(record.id,record);
    for(const id of page.removed_ids)records.delete(id);
    if(!page.done&&page.next_offset<=offset)throw new Error('同步分页没有前进');
    offset=page.next_offset;if(page.done)break;
   }
   await api('/sync/ack','POST',{cursor:snapshot.watermark,snapshot_id:snapshot.snapshot_id});
   let cursor=snapshot.watermark;
   while(true){
    const page=await api('/sync/changes?after='+cursor+'&limit=100');
    for(const record of page.records)records.set(record.id,record);
    for(const id of page.removed_ids)records.delete(id);
    if(page.has_more&&page.next_cursor<=cursor)throw new Error('增量同步游标没有前进');
    cursor=page.next_cursor;await api('/sync/ack','POST',{cursor});
    if(!page.has_more)return {records:[...records.values()]};
   }
  }catch(error){
   if(attempt===0&&error instanceof APIError&&[404,409,410].includes(error.status))continue;
   throw error;
  }
 }
 throw new Error('同步状态已变化，请刷新');
}
