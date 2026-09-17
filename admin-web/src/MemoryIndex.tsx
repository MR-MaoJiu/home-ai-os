import {useEffect,useState} from 'react';
import {api} from './api';

type IndexState={provider_id:string|null;eligible:number;ready:number;pending:number;status:string};
type DerivedState={provider_id:string;status:string;attempts:number;error_type:string|null};
const labels:Record<string,string>={READY:'已就绪',PENDING:'等待重建',FAILED:'同步失败',DISABLED:'已停用',UNCONFIGURED:'未配置模型',REBUILDING:'正在重建'};

export function MemoryIndex({revision,onChanged}:{revision:number;onChanged:()=>void}){
 const[index,setIndex]=useState<IndexState|null>(null);
 const[derived,setDerived]=useState<DerivedState[]>([]);
 const[error,setError]=useState('');
 const[busy,setBusy]=useState(false);
 useEffect(()=>{
  let active=true;
  Promise.all([api('/memory/index'),api('/memory/derived')]).then(([index,derived])=>{
   if(active){setIndex(index);setDerived(derived);setError('')}
  }).catch(error=>{if(active)setError(error instanceof Error?error.message:'无法读取索引状态')});
  return()=>{active=false};
 },[revision]);
 async function rebuild(){
  if(!confirm('重新生成你自己的记忆向量？规范数据不会删除，重建期间使用文字检索。'))return;
  setBusy(true);setError('');
  try{await api('/memory/index/rebuild','POST');onChanged()}
  catch(error){setError(error instanceof Error?error.message:'无法提交重建')}
  finally{setBusy(false)}
 }
 return <section><div className="section-title"><h2>记忆索引</h2><button onClick={onChanged}>刷新状态</button></div>
  {error&&<p role="alert" className="message error">{error}</p>}
  {index?<><p>{labels[index.status]??index.status} · {index.provider_id??'请先配置本地 Embedding Provider'}</p>
   <div className="summary"><div><span>可索引事实</span><strong>{index.eligible}</strong></div><div><span>已就绪</span><strong>{index.ready}</strong></div><div><span>待重建</span><strong>{index.pending}</strong></div></div>
   <p>状态只统计当前账户的记忆。重建由服务器的记忆工作进程执行。</p>
   <button disabled={busy||!index.provider_id} onClick={rebuild}>{busy?'正在提交…':'重建我的向量索引'}</button></>:<p>正在读取状态…</p>}
  {derived.length>0&&<><h3>外部派生索引</h3>{derived.map(row=><div className="row" key={row.provider_id}><b>{row.provider_id}</b><span>{labels[row.status]??row.status}</span><span>累计重建 {row.attempts} 次</span>{row.error_type&&<span>{row.error_type}</span>}</div>)}</>}
 </section>
}
