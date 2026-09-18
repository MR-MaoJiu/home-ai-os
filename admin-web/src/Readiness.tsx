import {useState} from 'react';
import {api} from './api';
type Report={core_dependencies_ready:boolean;runtime_ready:boolean;checked_at:number;checks:Record<string,{ok:boolean;reason:string}>;unverified:string[]};
const names:Record<string,string>={database:'数据库与权限隔离',policy:'策略决策',events:'事件服务',encryption:'加密服务',workers:'后台执行器'};
export function Readiness(){
 const[report,setReport]=useState<Report|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
 async function check(){
  setBusy(true);setError('');setReport(null);
  try{const result=await api<Report>('/manage/readiness');if(!result.checks||typeof result.core_dependencies_ready!=='boolean')throw new Error('服务器尚未支持详细诊断，请先更新并重启服务');setReport(result)}
  catch(error){setError(error instanceof Error?error.message:'部署检查失败')}
  finally{setBusy(false)}
 }
 return <section><h2>部署检查</h2><p>只读检查基础依赖。通过不代表模型、worker、生产隔离或家庭部署已经验收。</p>
  <button disabled={busy} onClick={check}>{busy?'正在检查…':'检查部署条件'}</button>
  {error&&<p role="alert">{error}</p>}
  {report&&<><p role="status">{report.runtime_ready?'基础依赖与后台循环检查通过':report.core_dependencies_ready?'基础依赖通过，后台执行器未就绪':'部分基础依赖未就绪'} · {new Date(report.checked_at*1000).toLocaleString()}</p>
   {Object.entries(report.checks).map(([key,check])=><div className="row" key={key}><b>{names[key]??key}</b><span>{check.ok?'通过':'未通过'}</span><span>{check.reason}</span></div>)}
   <h3>仍需独立验收</h3><ul>{report.unverified.map(item=><li key={item}>{item}</li>)}</ul></>}
 </section>;
}
