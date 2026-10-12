import {useEffect,useRef,useState} from 'react';
import {api} from './api';
import {MAX_FINITE_TOKEN_LIMIT,parseTokenLimit,formatTokenLimit,type TokenLimit} from './budgetLimits';

type Limits={daily_tokens:TokenLimit;daily_images:number};
type Overrides={daily_tokens:TokenLimit|null;daily_images:number|null};
type MemberBudget={
 id:string;name:string;role:string;overrides:Overrides;limits:Limits;
 usage:{tokens:number;images:number};remaining:{tokens:number|null;images:number};
};
type BudgetOverview={
 day:string;defaults:Limits;
 household:Limits&{used_tokens:number;used_images:number};
 members:MemberBudget[];
};
type Draft={daily_tokens:string|null;daily_images:string|null};
const labels:Record<string,string>={infrastructure_owner:'家庭管理员',adult:'成年成员',child:'儿童成员',minor:'未成年成员'};
const number=(value:number)=>value.toLocaleString('zh-CN');
function draftFor(member:MemberBudget):Draft{
 return {daily_tokens:member.overrides.daily_tokens===null?null:String(member.overrides.daily_tokens),
  daily_images:member.overrides.daily_images===null?null:String(member.overrides.daily_images)};
}
function parseLimit(value:string|null,maximum:number,label:string):number|null{
 if(value===null)return null;
 if(!/^\d+$/.test(value)||!Number.isSafeInteger(Number(value))||Number(value)>maximum)throw new Error(label+'需填写 0–'+number(maximum)+' 的整数，或选择继承默认。');
 return Number(value);
}

export function MemberBudgets({memberID,onClose}:{memberID:string;onClose:()=>void}){
 const[data,setData]=useState<BudgetOverview|null>(null),[draft,setDraft]=useState<Draft|null>(null);
 const[loading,setLoading]=useState(true),[saving,setSaving]=useState(false),[revision,setRevision]=useState(0);
 const[error,setError]=useState(''),[notice,setNotice]=useState('');
 const mounted=useRef(true);
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[]);
 useEffect(()=>{
  let active=true;setLoading(true);setError('');
  api<BudgetOverview>('/manage/member-budgets').then(value=>{
   if(!active)return;
   setData(value);
   const member=value.members.find(item=>item.id===memberID);
   if(member)setDraft(previous=>previous??draftFor(member));
  }).catch(problem=>{if(active)setError((problem as Error).message)}).finally(()=>{if(active)setLoading(false)});
  return()=>{active=false};
 },[memberID,revision]);
 const member=data?.members.find(item=>item.id===memberID);
 const blocked=loading||saving;
 async function save(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();if(!draft||!member||blocked)return;
  setError('');setNotice('');
  let body:Overrides;
  try{body={daily_tokens:draft.daily_tokens===null?null:parseTokenLimit(draft.daily_tokens),daily_images:parseLimit(draft.daily_images,1000,'每日图片额度')}}
  catch(problem){setError((problem as Error).message);return}
  setSaving(true);
  try{
   const value=await api<MemberBudget>('/manage/member-budgets/'+encodeURIComponent(memberID),'PUT',body);
   if(!mounted.current)return;
   setData(previous=>previous?{...previous,members:previous.members.map(item=>item.id===value.id?value:item)}:previous);
   setDraft(draftFor(value));setNotice('已保存此成员的额度。家庭默认值和其他成员的设置未改变。');
   setRevision(previous=>previous+1);
  }catch(problem){if(mounted.current)setError((problem as Error).message)}
  finally{if(mounted.current)setSaving(false)}
 }
 return <section className="settings-form" aria-label="成员额度设置">
  <div className="section-title"><h2>{member?member.name+' · 每日额度':'成员每日额度'}</h2><button onClick={onClose} disabled={saving}>关闭</button></div>
  {error&&<div className="message error" role="alert">{notice?'额度已保存，刷新用量失败：'+error:error}</div>}
  {notice&&<div className="message" role="status">{notice}</div>}
  {loading&&!data?<p>正在读取成员额度…</p>:!member||!data?<p>{error?'暂时无法读取额度。':'此成员已不在当前家庭，无法设置额度。'}</p>:<>
   <p>{labels[member.role]??member.role} · 统计日期 {data.day}（UTC）。每天 UTC 00:00 重置，修改额度不会清空当天已占用量。</p>
   <div className="table-scroll member-budget-usage"><table><caption>当前成员用量（包含尚未结算的预留）</caption><thead><tr><th>项目</th><th>生效限额</th><th>已计入</th><th>当前可用</th></tr></thead><tbody>
    <tr><th scope="row">Token</th><td>{formatTokenLimit(member.limits.daily_tokens)}<small>{member.overrides.daily_tokens===null?'继承家庭默认':member.overrides.daily_tokens==='unlimited'?'单独不限':'自定义'}</small></td><td>{number(member.usage.tokens)}</td><td>{formatTokenLimit(member.remaining.tokens)}</td></tr>
    <tr><th scope="row">图片（张）</th><td>{number(member.limits.daily_images)}<small>{member.overrides.daily_images===null?'继承家庭默认':'自定义'}</small></td><td>{number(member.usage.images)}</td><td>{number(member.remaining.images)}</td></tr>
   </tbody></table></div>
   <p>家庭当天占用：Token {number(data.household.used_tokens)} / {formatTokenLimit(data.household.daily_tokens)}，图片 {number(data.household.used_images)} / {number(data.household.daily_images)} 张。当前可用仍受家庭与成员两侧额度约束；一侧不限而另一侧有限时，使用有限侧的剩余额度。占用包含尚未结算的预留，不等于服务商实际用量或金额。</p>
   {draft&&<form onSubmit={save}>
    <label>每日 Token 额度<select value={draft.daily_tokens===null?'inherit':draft.daily_tokens==='unlimited'?'unlimited':'custom'} disabled={blocked} onChange={event=>setDraft(previous=>previous?{...previous,daily_tokens:event.target.value==='inherit'?null:event.target.value==='unlimited'?'unlimited':typeof member.limits.daily_tokens==='number'?String(member.limits.daily_tokens):'0'}:previous)}><option value="inherit">继承家庭默认（{formatTokenLimit(data.defaults.daily_tokens)}）</option><option value="custom">自定义有限额度</option><option value="unlimited">不限</option></select></label>
    {draft.daily_tokens!==null&&draft.daily_tokens!=='unlimited'&&<label>自定义每日 Token 上限<input type="number" inputMode="numeric" min={0} max={MAX_FINITE_TOKEN_LIMIT} step={1} required value={draft.daily_tokens} disabled={blocked} onChange={event=>setDraft(previous=>previous?{...previous,daily_tokens:event.target.value}:previous)}/></label>}
    <label>每日图片额度<select value={draft.daily_images===null?'inherit':'custom'} disabled={blocked} onChange={event=>setDraft(previous=>previous?{...previous,daily_images:event.target.value==='inherit'?null:String(member.limits.daily_images)}:previous)}><option value="inherit">继承家庭默认（{number(data.defaults.daily_images)} 张）</option><option value="custom">自定义此成员额度</option></select></label>
    {draft.daily_images!==null&&<label>自定义每日图片张数上限<input type="number" inputMode="numeric" min={0} max={1000} step={1} required value={draft.daily_images} disabled={blocked} onChange={event=>setDraft(previous=>previous?{...previous,daily_images:event.target.value}:previous)}/></label>}
    <small>Token 可继承、自定义或选择不限；图片仍按张数限制。自定义为 0 会停用该成员对应的云模型调用或图片生成与编辑；继承时，家庭默认值变更会自动生效。成员选择不限仍受家庭有限额度约束，图片额度不替代云模型所需的 Token 额度。</small>
    <button className="primary" disabled={blocked}>{saving?'正在保存…':'保存此成员额度'}</button>
   </form>}
  </>}
  <button onClick={()=>setRevision(previous=>previous+1)} disabled={blocked}>{loading?'正在读取…':'刷新用量'}</button>
 </section>;
}
