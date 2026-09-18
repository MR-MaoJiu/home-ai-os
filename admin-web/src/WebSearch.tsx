import {useState} from 'react';
import {api} from './api';
export function WebSearch(){
 const[query,setQuery]=useState(''),[busy,setBusy]=useState(false),[message,setMessage]=useState(''),[error,setError]=useState('');
 async function submit(event:React.FormEvent){
  event.preventDefault();setBusy(true);setError('');setMessage('');
  try{
   const task=await api<{id:string}>('/tasks','POST',{idempotency_key:crypto.randomUUID(),capability:'web.search@v1',arguments:{query}});
   setMessage('搜索任务已提交（'+task.id.slice(0,8)+'）。服务器会自动执行，可在任务与审批中查看结果。');
  }catch(error){setError(error instanceof Error?error.message:'搜索提交失败')}
  finally{setBusy(false)}
 }
 return <section><h2>联网搜索</h2><p>查询会通过自托管 SearXNG 发送给外部搜索引擎，公开查询自动执行，仍经过隐私检查。不要包含个人信息、秘密或私人资料。</p>
  <form className="inline" onSubmit={submit}><input aria-label="公开搜索词" value={query} onChange={event=>setQuery(event.target.value)} maxLength={500} required placeholder="搜索公开资料"/><button className="primary" disabled={busy||!query.trim()}>{busy?'提交中…':'开始公开搜索'}</button></form>
  {message&&<p role="status">{message}</p>}{error&&<p role="alert">{error}</p>}
 </section>;
}
