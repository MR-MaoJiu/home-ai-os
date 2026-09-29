import {useEffect,useState} from 'react';
import {api} from './api';

type Role='local_fast'|'local_privacy'|'cloud_planner'|'vision';
type Configuration=Record<Role,string|null>&{shared_cloud:boolean;household_daily_tokens:number;member_daily_tokens:number};
type Provider={id:string;enabled:boolean;manifest:{cloud?:boolean;model?:string;capabilities:Record<string,string>}};
type Verification={id:string;text:boolean;tools:boolean;vision:boolean;privacy?:boolean;status:'configured'|'verified'|'unavailable';verified_at:number|null;errors?:Record<string,string>;resolved_model?:string};
type Models={configuration:Configuration;providers:Verification[]};
const roles:[Role,string,string][]=[['local_fast','本地简答','用于日常简答'],['local_privacy','本地隐私处理','用于敏感内容识别与脱敏'],['cloud_planner','云端规划','用于已获授权的复杂任务'],['vision','视觉理解','用于图片与视频内容理解']];
const stateNames:Record<Verification['status'],string>={configured:'已配置，尚未验证',verified:'已实测',unavailable:'本次验证不可用'};

export function ModelRouting({providers,revision,onVerified}:{providers:Provider[];revision:number;onVerified:(id:string,healthy:boolean)=>void}){
 const[config,setConfig]=useState<Configuration|null>(null),[reports,setReports]=useState<Verification[]>([]);
 const[busy,setBusy]=useState(false),[verifying,setVerifying]=useState<string|null>(null),[error,setError]=useState(''),[notice,setNotice]=useState('');
 useEffect(()=>{let active=true;api<Models>('/manage/models').then(value=>{if(active){setConfig(value.configuration);setReports(value.providers)}}).catch(e=>{if(active)setError((e as Error).message)});return()=>{active=false}},[revision]);
 const candidates=providers.filter(provider=>'model.generate@v1' in provider.manifest.capabilities);
 function options(role:Role){return candidates.filter(provider=>role.startsWith('local_')?!provider.manifest.cloud:role==='cloud_planner'?provider.manifest.cloud:true)}
 async function save(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();if(!config)return;setBusy(true);setError('');setNotice('');
  try{await api('/manage/models','PUT',config);setNotice('模型用途与 Token 限额已保存。能力调用仍以实际验证和用户授权为准。')}
  catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 async function verify(id:string){
  setVerifying(id);setError('');setNotice('');
  try{const report=await api<Omit<Verification,'id'>>('/manage/models/'+encodeURIComponent(id)+'/verify','POST');setReports(previous=>[...previous.filter(item=>item.id!==id),{id,...report}]);onVerified(id,report.text);setNotice('已完成本次真实能力探测，请查看各项结果。')}
  catch(e){setError((e as Error).message)}finally{setVerifying(null)}
 }
 return <section className="settings-form"><h2>模型用途与验证</h2><p>优先使用当前已接入的模型。选择用途不会更换模型套餐或提高价格；验证会向所选接口发送固定公开测试内容，可能产生少量调用费用。</p>
  {error&&<div className="message error" role="alert">{error}</div>}{notice&&<div className="message" role="status">{notice}</div>}
  {!config?<p>{error?'暂时无法读取模型用途。':'正在读取配置…'}</p>:<form onSubmit={save}>
   {roles.map(([role,label,description])=><label key={role}>{label}<select value={config[role]??''} disabled={busy||verifying!==null} onChange={e=>setConfig({...config,[role]:e.target.value||null})}><option value="">{role.startsWith('local_')?'使用已启用本地模型':'暂不指定'}</option>{options(role).map(provider=><option key={provider.id} value={provider.id}>{provider.id}{provider.manifest.model?' · '+provider.manifest.model:''}{!provider.enabled?'（已停用）':''}</option>)}{config[role]&&!options(role).some(provider=>provider.id===config[role])&&<option value={config[role]!}>{config[role]}（不可用，请重新选择）</option>}</select><small>{description}。{role==='vision'?'云端视觉需要单独授权，不会自动上传私人附件。':role==='cloud_planner'?'需通过工具调用验证，并遵循云端披露与预算策略。':'本地用途仅可选择本地模型。'}</small></label>)}
   <label className="settings-toggle"><input type="checkbox" checked={config.shared_cloud} onChange={e=>setConfig({...config,shared_cloud:e.target.checked})} disabled={busy||verifying!==null}/>允许家庭成员共用配置的云模型服务</label>
   <label>家庭每日 Token 限额<input type="number" min={config.shared_cloud?1:0} max={100000000} step={1} required value={config.household_daily_tokens} onChange={e=>setConfig({...config,household_daily_tokens:Number(e.target.value)})} disabled={busy}/></label>
   <label>每位成员每日 Token 限额<input type="number" min={config.shared_cloud?1:0} max={10000000} step={1} required value={config.member_daily_tokens} onChange={e=>setConfig({...config,member_daily_tokens:Number(e.target.value)})} disabled={busy}/></label>
   <small>Token 是模型处理的文本单位，不是人民币或账单金额，每日按 UTC 零点重置。启用家庭共用必须选择云端规划模型，并设置两项正数限额；共用服务凭据不会共享成员私人数据。</small>
   <button className="primary" disabled={busy||verifying!==null||(config.shared_cloud&&(!config.cloud_planner||config.household_daily_tokens<=0||config.member_daily_tokens<=0))}>{busy?'正在保存…':'保存用途与限额'}</button>
  </form>}
  <div className="table-scroll"><table><thead><tr><th>模型</th><th>文本</th><th>工具</th><th>视觉</th><th>隐私检测</th><th>验证</th></tr></thead><tbody>{candidates.map(provider=>{const report=reports.find(item=>item.id===provider.id);return <tr key={provider.id}><td><b>{provider.id}</b><small>{report?.resolved_model??provider.manifest.model}</small></td>{(['text','tools','vision','privacy'] as const).map(capability=><td key={capability}>{capability==='privacy'&&provider.manifest.cloud?'仅本地执行':report?.verified_at?(report[capability]?'通过':'未通过'):'未验证'}{report?.errors?.[capability]&&<small>{report.errors[capability]}</small>}</td>)}<td>{stateNames[report?.status??'configured']}{report?.verified_at&&<small>{new Date(report.verified_at*1000).toLocaleString()}</small>}<button disabled={busy||verifying!==null} onClick={()=>verify(provider.id)}>{verifying===provider.id?'正在实测…':'验证能力'}</button></td></tr>})}</tbody></table>{!candidates.length&&<p className="empty">先在下方接入模型或启用内置本地模型。</p>}</div>
 </section>;
}
