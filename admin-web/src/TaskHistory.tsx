import {useState} from 'react';
import {api} from './api';

type Task={id:string;status:string;error?:string;execution?:{agent:boolean;planned_steps:number;max_steps:number;model_rounds:number;model_token_charge:number;max_model_tokens:number|null}};
type Step={id:string;step:number;capability:string;status:string;result:unknown};
export function TaskHistory({tasks,onChanged}:{tasks:Task[]|null;onChanged:()=>void}){
 const[selected,setSelected]=useState<Task|null>(null),[steps,setSteps]=useState<Step[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 async function inspect(task:Task){
  setBusy(true);setError('');
  try{const[current,items]=await Promise.all([api('/tasks/'+task.id),api('/tasks/'+task.id+'/steps')]);setSelected(current);setSteps(items)}
  catch(e){setError(e instanceof Error?e.message:'读取失败')}
  finally{setBusy(false)}
 }
 async function cancel(){
  if(!selected||!confirm('取消未完成步骤？已经完成的外部操作不能自动撤销。'))return;
  setBusy(true);setError('');
  try{await api('/tasks/'+selected.id+'/cancel','POST');await inspect(selected);onChanged()}
  catch(e){setError(e instanceof Error?e.message:'取消失败')}
  finally{setBusy(false)}
 }
 async function reconcile(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();if(!selected)return;
  const values=new FormData(event.currentTarget);
  if(!confirm('确认你已在外部系统核对操作结果？此记录将标记为人工核对，不是 Provider 自动确认。'))return;
  setBusy(true);setError('');
  try{
   const result=JSON.parse(String(values.get('result')||'{}'));
   await api('/tasks/'+selected.id+'/reconcile','POST',{decision:values.get('decision'),evidence:values.get('evidence'),result});
   await inspect(selected);onChanged();
  }catch(e){setError(e instanceof Error?e.message:'核对提交失败')}
  finally{setBusy(false)}
 }
 return <section><h2>最近任务</h2>{error&&<p className="message error" role="alert">{error}</p>}
  {!tasks?.length?<p className="empty">暂无任务</p>:tasks.map(task=><div className="row" key={task.id}><span>{task.id.slice(0,8)}</span><span>{task.status}</span><span>{task.error}</span><button disabled={busy} onClick={()=>inspect(task)}>查看步骤</button></div>)}
  {selected&&<div><h3>任务 {selected.id.slice(0,8)} · {selected.status}</h3>{selected.error&&<p className="message error">{selected.error}</p>}<p>已完成步骤会保留；取消和重启不会自动撤销已经发生的外部操作。</p>
   <button disabled={busy} onClick={()=>inspect(selected)}>刷新步骤</button>
   {selected.execution?.agent&&<p>本地 Agent · 已规划 {selected.execution.planned_steps}/{selected.execution.max_steps} 步 · 模型 {selected.execution.model_rounds} 轮 · Token 计入 {selected.execution.model_token_charge}/{selected.execution.max_model_tokens}（包含未知消耗的保守预留）</p>}
   {['RECEIVED','APPROVED','AWAITING_APPROVAL','EXECUTING'].includes(selected.status)&&<button disabled={busy} onClick={cancel}>取消后续执行</button>}
   {steps.map(step=><details key={step.id}><summary>步骤 {step.step+1} · {step.capability} · {step.status==='SKIPPED'?'条件不满足，已跳过':step.status}</summary>{step.status==='SKIPPED'?<p>此步骤没有调用工具，也没有生成执行结果。</p>:<pre>{JSON.stringify(step.result,null,2)}</pre>}</details>)}
   {selected.status==='NEEDS_RECONCILIATION'&&<form onSubmit={reconcile}><h3>外部结果人工核对</h3><p>请先查阅外部系统记录。确认未执行后，高风险操作仍需重新审批；过期或已取消的任务不能重新执行。</p>
    <label>核对结论<select name="decision"><option value="ABORT">停止，不再继续</option><option value="COMPLETED">外部已完成，记录结果</option><option value="NOT_EXECUTED">确定未执行，重新进入执行流程</option></select></label>
    <label>核对依据<textarea name="evidence" minLength={10} maxLength={2000} required placeholder="记录外部系统中的状态与核对依据，不要填入密码或 Token"/></label>
    <label>已确认的结果（JSON，可留空）<textarea name="result" placeholder="{}"/></label><button className="primary" disabled={busy}>提交人工核对</button>
   </form>}
  </div>}
 </section>
}
