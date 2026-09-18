import {useState} from 'react';
import {api} from './api';

type Rule={id:string;name:string;trigger_kind:'cron'|'event';cron:string;event_type:string|null;enabled:boolean;cooldown_seconds:number};
type Delivery={id:string;status:string;reason:string|null;task_id:string|null;created_at:number};
const events:Record<string,string>={'record.changed':'数据新增或更新','record.deleted':'数据删除','record.revoked':'共享授权撤回'};
const states:Record<string,string>={PENDING:'排队中',DISPATCHED:'已创建任务',SKIPPED:'已跳过',CANCELED:'已取消'};

export function Automations({rules,onChanged}:{rules:Rule[];onChanged:()=>void}){
 const[advanced,setAdvanced]=useState(false);
 const[trigger,setTrigger]=useState<'cron'|'event'>('cron');
 const[busy,setBusy]=useState(false),[error,setError]=useState('');
 const[history,setHistory]=useState<{name:string;rows:Delivery[]}|null>(null);
 async function create(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();const form=new FormData(event.currentTarget);
  setBusy(true);setError('');
  try{
   const title=String(form.get('title'));
   const steps=advanced?JSON.parse(String(form.get('steps'))):[{capability:'reminder.create@v1',arguments:{title,...(trigger==='event'?{source_record:{$event:'record_id'}}:{})}}];
   if(!Array.isArray(steps)||steps.length<1||steps.length>16)throw new Error('步骤必须是包含 1 至 16 项的 JSON 数组');
   await api('/automations','POST',{name:title,trigger_kind:trigger,enabled:true,
    timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
    ...(trigger==='cron'?{cron:form.get('cron')}:{event_type:form.get('event'),
      record_kind:String(form.get('kind')||'')||null,record_source:String(form.get('source')||'')||null,
      include_shared:form.get('shared')==='on',cooldown_seconds:Number(form.get('cooldown'))}),
    skill:{name:title,steps}});
   onChanged();
  }catch(error){setError(error instanceof Error?error.message:'创建失败')}
  finally{setBusy(false)}
 }
 async function stop(rule:Rule){
  setBusy(true);setError('');
  try{await api('/automations/'+rule.id,'DELETE');setHistory(null);onChanged()}
  catch(error){setError(error instanceof Error?error.message:'停用失败')}
  finally{setBusy(false)}
 }
 async function showHistory(rule:Rule){
  setBusy(true);setError('');setHistory(null);
  try{setHistory({name:rule.name,rows:await api('/automations/'+rule.id+'/deliveries')})}
  catch(error){setError(error instanceof Error?error.message:'读取失败')}
  finally{setBusy(false)}
 }
 return <><section><h2>我的自动化</h2><p>自动化是“什么时候、做什么”的规则。例如每天 8 点创建提醒；触发后会生成任务，再到“任务与审批”查看执行或确认。这里管理当前账号的规则，不展示其他成员的私人自动化。事件仅携带记录标识，执行仍受权限约束。</p>
  {error&&<p role="alert">{error}</p>}
  <form onSubmit={create}><fieldset disabled={busy}>
   <label className="field">名称或提醒内容<input name="title" required maxLength={100}/></label>
   <label className="field">触发方式<select value={trigger} onChange={e=>setTrigger(e.target.value as 'cron'|'event')}><option value="cron">定时</option><option value="event">数据事件</option></select></label>
   {trigger==='cron'?<label className="field">Cron 表达式<input name="cron" placeholder="0 8 * * *" required/></label>:<>
    <label className="field">事件<select name="event">{Object.entries(events).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label>
    <label className="field">数据类型<input name="kind" placeholder="如 document.parsed，留空为所有类型" maxLength={100}/></label>
    <label className="field">来源<input name="source" placeholder="留空为所有来源" maxLength={100}/></label>
    <label className="field">触发间隔（秒）<input name="cooldown" type="number" min={0} max={86400} defaultValue={60} required/></label>
    <label><input name="shared" type="checkbox"/>包含别人明确共享给我的数据</label>
    <p>间隔内的事件保留排队；处理前会复核权限与版本。停用会取消尚未创建任务的投递。已创建的任务请在任务页面取消。</p>
   </>}
   <label><input type="checkbox" checked={advanced} onChange={e=>setAdvanced(e.target.checked)}/>配置多步骤工作流</label>
   {advanced&&<label className="field">步骤 JSON 数组<textarea name="steps" rows={12} required defaultValue={'[\n  {"capability":"calendar.search@v1","arguments":{"query":"会议"}},\n  {"capability":"reminder.create@v1","arguments":{"title":"准备会议资料"},"when":{"mode":"all","predicates":[{"step":0,"operator":"not_empty"}]}}\n]'}/><small>步骤编号从 0 开始。when 只允许固定条件运算；条件不满足会跳过，工具仍经过权限与审批检查。这里不支持脚本。</small></label>}
   <button className="primary" type="submit">{busy?'处理中…':'创建自动化'}</button>
  </fieldset></form>
 </section><section><h2>已有规则</h2>{rules.length?rules.map(rule=><div className="row" key={rule.id}>
  <b>{rule.name}</b><span>{rule.trigger_kind==='event'?events[rule.event_type??'']:rule.cron}</span>
  <span>{rule.enabled?'已启用':'已停用'}</span>
  {rule.trigger_kind==='event'&&<button disabled={busy} onClick={()=>showHistory(rule)}>投递记录</button>}
  {rule.enabled&&<button disabled={busy} onClick={()=>stop(rule)}>停用</button>}
 </div>):<p>尚未配置规则</p>}</section>
 {history&&<section><h2>{history.name} · 最近投递</h2><p>“已创建任务”表示交给任务系统；任务是否执行成功请在任务页面查看。</p>{history.rows.length?history.rows.map(row=><div className="row" key={row.id}><time>{new Date(row.created_at*1000).toLocaleString()}</time><span>{states[row.status]??row.status}</span><span>{row.reason??row.task_id??'等待调度'}</span></div>):<p>暂无事件投递</p>}</section>}
 </>;
}
