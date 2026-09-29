export class APIError extends Error { constructor(public readonly status:number,message:string){super(message)} }
export async function api<T=any>(path:string,method='GET',body?:unknown):Promise<T>{
 const csrf=document.cookie.split('; ').find(s=>s.startsWith('__Host-homeai-csrf='))?.split('=').slice(1).join('=')??'';
 const r=await fetch('/api/v1'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(csrf)},body:body===undefined?undefined:JSON.stringify(body)});
 const value=await r.json();
 if(!r.ok){
  const labels:Record<string,string>={'body.id':'Provider ID','body.endpoint':'接口地址','body.model':'模型名称','body.provider_id':'凭据所属 Provider','body.value':'API Key','body.allowed_hosts':'允许访问的主机','body.secret_id':'凭据标识'};
  const fields=Array.isArray(value.fields)?value.fields.filter((field:unknown)=>typeof field==='string').map((field:string)=>labels[field]??field):[];
  throw new APIError(r.status,(typeof value.detail==='string'?value.detail:'操作未完成')+(fields.length?'：请检查'+fields.join('、'):''));
 }
 return value;
}
