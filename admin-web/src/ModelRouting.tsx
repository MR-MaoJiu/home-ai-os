import {useEffect,useState} from 'react';
import {api} from './api';
import {MAX_FINITE_TOKEN_LIMIT,parseTokenLimit,type TokenLimit} from './budgetLimits';

type Role='local_fast'|'local_privacy'|'cloud_planner'|'vision'|'image_generation'|'image_editing';
type ImageCapability='image_generation'|'image_editing';
type Capability='text'|'tools'|'vision'|'privacy'|ImageCapability;
type Configuration=Record<Role,string|null>&{cloud_planner_thinking?:'provider_default'|'disabled';shared_cloud:boolean;household_daily_tokens:TokenLimit;member_daily_tokens:TokenLimit;household_daily_images:number;member_daily_images:number};
type Provider={id:string;enabled:boolean;manifest:{cloud?:boolean;model?:string;capabilities:Record<string,string>}};
type Verification={id:string;text:boolean;tools:boolean;vision:boolean;privacy?:boolean;image_generation?:boolean;image_editing?:boolean;status:'configured'|'verified'|'unavailable';verified_at:number|null;errors?:Record<string,string>;resolved_model?:string};
type Models={configuration:Configuration;providers:Verification[]};
const imageCapabilities:Record<ImageCapability,string>={image_generation:'image.generate@v1',image_editing:'image.edit@v1'};
const capabilityLabels:[Capability,string][]=[['text','文本'],['tools','工具'],['vision','图片理解'],['privacy','隐私检测'],['image_generation','图片生成'],['image_editing','图片编辑']];
const roles:[Role,string,string][]=[
 ['local_fast','本地简答','用于日常简答，仅选择本地模型。'],
 ['local_privacy','本地隐私处理','用于敏感内容识别与脱敏，仅选择本地模型。'],
 ['cloud_planner','云端规划','需通过工具调用验证，并遵循云端披露与 Token 预算策略。'],
 ['vision','图片与视频理解','分析已有图片或视频。使用云模型需要单独授权，不会自动上传私人附件。'],
 ['image_generation','图片生成','根据提示生成新图片，使用 Images Generations 接口；与图片理解能力分开配置。'],
 ['image_editing','图片编辑','按要求修改已授权的图片，使用 Images Edits 接口。'],
];
const stateNames:Record<Verification['status'],string>={configured:'已配置，尚未实测',verified:'已有能力通过实测',unavailable:'本次验证不可用'};
const hasImages=(provider:Provider)=>Object.values(imageCapabilities).some(capability=>capability in provider.manifest.capabilities);
const hasChat=(provider:Provider)=>'model.generate@v1' in provider.manifest.capabilities;
const healthy=(report:Verification)=>report.text||!!report.image_generation||!!report.image_editing;

export function ModelRouting({providers,revision,onVerified}:{providers:Provider[];revision:number;onVerified:(id:string,healthy:boolean)=>void}){
 const[config,setConfig]=useState<Configuration|null>(null),[reports,setReports]=useState<Verification[]>([]);
 const[tokenDraft,setTokenDraft]=useState({household:'0',member:'0'});
 const[busy,setBusy]=useState(false),[verifying,setVerifying]=useState<string|null>(null),[error,setError]=useState(''),[notice,setNotice]=useState('');
 useEffect(()=>{let active=true;api<Models>('/manage/models').then(value=>{if(active){setConfig(value.configuration);setTokenDraft({household:typeof value.configuration.household_daily_tokens==='number'?String(value.configuration.household_daily_tokens):'0',member:typeof value.configuration.member_daily_tokens==='number'?String(value.configuration.member_daily_tokens):'0'});setReports(value.providers)}}).catch(e=>{if(active)setError((e as Error).message)});return()=>{active=false}},[revision]);
 const candidates=providers.filter(provider=>hasChat(provider)||hasImages(provider));
 const reportByID=new Map(reports.map(report=>[report.id,report]));
 function options(role:Role){
  if(role==='image_generation'||role==='image_editing')return candidates.filter(provider=>imageCapabilities[role] in provider.manifest.capabilities);
  return candidates.filter(provider=>hasChat(provider)&&(role.startsWith('local_')?!provider.manifest.cloud:role==='cloud_planner'?provider.manifest.cloud:true));
 }
 async function save(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();if(!config)return;setBusy(true);setError('');setNotice('');
  try{const body={...config,household_daily_tokens:parseTokenLimit(config.household_daily_tokens==='unlimited'?'unlimited':tokenDraft.household),member_daily_tokens:parseTokenLimit(config.member_daily_tokens==='unlimited'?'unlimited':tokenDraft.member)};await api('/manage/models','PUT',body);setConfig(body);setNotice('模型用途、Token 与图片张数限额已保存。实际调用仍须满足用户授权与服务端策略。')}
  catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 async function verify(id:string,image?:ImageCapability){
  if(image&&!window.confirm(`将对 ${id} 实际${image==='image_generation'?'生成':'编辑'}一张公开测试图片，可能产生模型调用费用。是否继续？`))return;
  setVerifying(id+':'+(image??'chat'));setError('');setNotice('');
  try{
   const path='/manage/models/'+encodeURIComponent(id)+(image?'/verify-images':'/verify');
   const value=await api<Omit<Verification,'id'>>(path,'POST',image?{generate:image==='image_generation',edit:image==='image_editing',confirm_usage:true}:undefined);
   const report={id,...value};setReports(previous=>[...previous.filter(item=>item.id!==id),report]);onVerified(id,healthy(report));
   setNotice('本次实测已结束，请查看各项能力结果；未测试的图片能力仍显示未验证。');
  }catch(e){setError((e as Error).message)}finally{setVerifying(null)}
 }
 function capabilityState(provider:Provider,report:Verification|undefined,capability:Capability){
  const image=capability==='image_generation'||capability==='image_editing';
  if(image&&!(imageCapabilities[capability] in provider.manifest.capabilities)||!image&&!hasChat(provider))return '未配置';
  if(capability==='privacy'&&provider.manifest.cloud)return '仅本地执行';
  if(report?.[capability])return '通过';
  if(report?.errors?.[capability])return '未通过';
  return '未验证';
 }
 return <section className="settings-form"><h2>模型用途与验证</h2>
  <p>优先使用当前已接入的模型。保存配置不会调用模型；对话能力验证会发送公开测试内容，图片能力验证会实际生成或编辑测试图片，两者均可能产生服务商费用。</p>
  {error&&<div className="message error" role="alert">{error}</div>}{notice&&<div className="message" role="status">{notice}</div>}
  {!config?<p>{error?'暂时无法读取模型用途。':'正在读取配置…'}</p>:<form onSubmit={save}>
   {roles.map(([role,label,description])=>{const available=options(role);return <label key={role}>{label}<select value={config[role]??''} disabled={busy||verifying!==null} onChange={e=>setConfig({...config,[role]:e.target.value||null})}><option value="">{role.startsWith('local_')?'使用已启用本地模型':'暂不指定'}</option>{available.map(provider=><option key={provider.id} value={provider.id}>{provider.id}{provider.manifest.model?' · '+provider.manifest.model:''}{!provider.enabled?'（已停用）':''}</option>)}{config[role]&&!available.some(provider=>provider.id===config[role])&&<option value={config[role]!}>{config[role]}（不可用，请重新选择）</option>}</select><small>{description}</small></label>})}
   <label>云端规划思考模式<select value={config.cloud_planner_thinking??'provider_default'} disabled={busy||verifying!==null} onChange={e=>setConfig({...config,cloud_planner_thinking:e.target.value==='disabled'?'disabled':'provider_default'})}><option value="provider_default">服务商默认</option><option value="disabled">关闭思考</option></select><small>当前云端单次输出上限为 2,048 Token，推理模型可能把这部分预算用于思考。“关闭思考”仅适用于支持 DeepSeek thinking.type 的兼容服务；其他接口请保留服务商默认。此设置只作用于选定的云端规划模型。</small></label>
   <label className="settings-toggle"><input type="checkbox" checked={config.shared_cloud} onChange={e=>setConfig({...config,shared_cloud:e.target.checked})} disabled={busy||verifying!==null}/>允许家庭成员共用配置的云模型服务</label>
   <label>家庭每日 Token 额度<select value={config.household_daily_tokens==='unlimited'?'unlimited':'finite'} disabled={busy} onChange={e=>setConfig({...config,household_daily_tokens:e.target.value==='unlimited'?'unlimited':0})}><option value="finite">有限额度</option><option value="unlimited">不限</option></select></label>
   {config.household_daily_tokens!=='unlimited'&&<label>家庭每日 Token 上限<input type="number" inputMode="numeric" min={config.shared_cloud?1:0} max={MAX_FINITE_TOKEN_LIMIT} step={1} required value={tokenDraft.household} onChange={e=>setTokenDraft(previous=>({...previous,household:e.target.value}))} disabled={busy}/></label>}
   <label>成员默认每日 Token 额度<select value={config.member_daily_tokens==='unlimited'?'unlimited':'finite'} disabled={busy} onChange={e=>setConfig({...config,member_daily_tokens:e.target.value==='unlimited'?'unlimited':0})}><option value="finite">有限额度</option><option value="unlimited">不限</option></select></label>
   {config.member_daily_tokens!=='unlimited'&&<label>成员默认每日 Token 上限<input type="number" inputMode="numeric" min={0} max={MAX_FINITE_TOKEN_LIMIT} step={1} required value={tokenDraft.member} onChange={e=>setTokenDraft(previous=>({...previous,member:e.target.value}))} disabled={busy}/></label>}
   <small>Token 额度不是人民币或账单金额，每日按 UTC 零点重置。家庭与成员均选择“不限”时不设每日 Token 上限；任一侧有限，仍受该侧额度限制。启用家庭共用需选择云端规划模型，家庭额度为正数或不限。成员默认可设为 0，停用未单独设置额度的成员；在“成员与设备 → 额度设置”中可为指定成员单独开通。共用服务凭据不会共享成员私人数据。</small>
   <label>家庭每日图片张数限额<input type="number" min={0} max={10000} step={1} required value={config.household_daily_images??0} onChange={e=>setConfig({...config,household_daily_images:Number(e.target.value)})} disabled={busy}/></label>
   <label>成员默认每日图片张数限额<input type="number" min={0} max={1000} step={1} required value={config.member_daily_images??0} onChange={e=>setConfig({...config,member_daily_images:Number(e.target.value)})} disabled={busy}/></label>
   <small>生成与编辑共用图片张数额度。成员默认值可在“成员与设备 → 额度设置”中单独覆盖；家庭或成员生效限额为 0 张时关闭对应图片请求；不是金额预算，也不由 Token 额度替代。配置对应用途、启用 Provider 并设置两项正数额度后可发起请求。首次调用前保留“未验证”状态，只有真实成功后才标记能力通过。</small>
   <button className="primary" disabled={busy||verifying!==null||(config.shared_cloud&&(!config.cloud_planner||(config.household_daily_tokens!=='unlimited'&&Number(tokenDraft.household)===0)))}>{busy?'正在保存…':'保存用途与限额'}</button>
  </form>}
  <div className="table-scroll"><table><thead><tr><th>模型</th>{capabilityLabels.map(([capability,label])=><th key={capability}>{label}</th>)}<th>验证</th></tr></thead><tbody>{candidates.map(provider=>{
   const report=reportByID.get(provider.id);
   return <tr key={provider.id}><td><b>{provider.id}</b><small>{report?.resolved_model??provider.manifest.model}</small></td>{capabilityLabels.map(([capability])=><td key={capability}>{capabilityState(provider,report,capability)}{report?.errors?.[capability]&&<small>{report.errors[capability]}</small>}</td>)}<td>{stateNames[report?.status??'configured']}{report?.verified_at&&<small>{new Date(report.verified_at*1000).toLocaleString()}</small>}
    {hasChat(provider)&&<button disabled={busy||verifying!==null} onClick={()=>verify(provider.id)}>{verifying===provider.id+':chat'?'正在实测…':'验证对话能力'}</button>}
    {(['image_generation','image_editing'] as const).filter(capability=>imageCapabilities[capability] in provider.manifest.capabilities).map(capability=><button key={capability} disabled={busy||verifying!==null} onClick={()=>verify(provider.id,capability)}>{verifying===provider.id+':'+capability?'正在实测…':`验证图片${capability==='image_generation'?'生成':'编辑'}（会产生模型调用费用）`}</button>)}
   </td></tr>;
  })}</tbody></table>{!candidates.length&&<p className="empty">先在下方接入模型或启用内置本地模型。</p>}</div>
 </section>;
}
