export async function api<T=any>(path:string,method='GET',body?:unknown):Promise<T>{
 const csrf=document.cookie.split('; ').find(s=>s.startsWith('__Host-homeai-csrf='))?.split('=').slice(1).join('=')??'';
 const r=await fetch('/api/v1'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(csrf)},body:body===undefined?undefined:JSON.stringify(body)});
 const value=await r.json(); if(!r.ok)throw new Error(typeof value.detail==='string'?value.detail:'操作未完成'); return value;
}

export async function uploadDocument(file:File):Promise<{id:string}>{
 const csrf=document.cookie.split('; ').find(s=>s.startsWith('__Host-homeai-csrf='))?.split('=').slice(1).join('=')??'';
 const form=new FormData();form.append('file',file);
 const response=await fetch('/api/v1/files',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':decodeURIComponent(csrf)},body:form});
 const value=await response.json();
 if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:'上传失败');
 return value;
}
