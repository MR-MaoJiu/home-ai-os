export async function api<T=any>(path:string,method='GET',body?:unknown):Promise<T>{
 const csrf=document.cookie.split('; ').find(s=>s.startsWith('__Host-homeai-csrf='))?.split('=').slice(1).join('=')??'';
 const r=await fetch('/api/v1'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(csrf)},body:body===undefined?undefined:JSON.stringify(body)});
 const value=await r.json(); if(!r.ok)throw new Error(typeof value.detail==='string'?value.detail:'操作未完成'); return value;
}
