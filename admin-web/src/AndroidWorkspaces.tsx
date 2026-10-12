import {useEffect,useRef,useState} from 'react';
import {APIError,api} from './api';
import './AndroidWorkspaces.css';

type AndroidBackend='physical'|'emulator';
type AndroidStatus='online'|'offline'|'unauthorized';
type Member={id:string;name:string};
type AndroidTransport='usb'|'wireless'|'offline';
type AndroidDevice={serial:string;label:string;backend:AndroidBackend;status:AndroidStatus;transport?:AndroidTransport;wireless_configured?:boolean};
type AndroidBinding={member_id:string;workspace_id:string;label:string;backend:AndroidBackend;status:AndroidStatus;packages:string[];serial?:string;transport?:AndroidTransport;wireless_configured?:boolean};
type Draft={memberID:string;serial:string;label:string;packages:string};
const emptyDraft:Draft={memberID:'',serial:'',label:'',packages:''};
const backendLabels:Record<AndroidBackend,string>={physical:'专用真机',emulator:'模拟器'};
const statusLabels:Record<AndroidStatus,string>={online:'在线',offline:'离线',unauthorized:'等待 USB 调试授权'};
const statusLabel=(status:AndroidStatus)=>statusLabels[status]??'状态未知';
const transportLabel=(transport?:AndroidTransport)=>transport==='wireless'?'无线连接':transport==='usb'?'USB 连接':'未连接';
const backendLabel=(backend:AndroidBackend)=>backendLabels[backend]??'未知设备类型';
const messageFor=(problem:unknown)=>problem instanceof APIError&&problem.status===403?'仅家庭管理员可以管理安卓设备绑定。':problem instanceof Error?problem.message:'暂时无法读取安卓设备，请稍后重试。';

export function AndroidWorkspaces({members,revision}:{members:Member[];revision:number}){
 const[devices,setDevices]=useState<AndroidDevice[]>([]),[bindings,setBindings]=useState<AndroidBinding[]>([]);
 const[draft,setDraft]=useState<Draft>(emptyDraft),[loading,setLoading]=useState(true),[loaded,setLoaded]=useState(false),[saving,setSaving]=useState(false),[refresh,setRefresh]=useState(0);
 const[error,setError]=useState(''),[notice,setNotice]=useState('');
 const mounted=useRef(true);
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[]);
 useEffect(()=>{
  let active=true;setLoading(true);setError('');setLoaded(false);
  Promise.all([
   api<{devices:AndroidDevice[]}>('/manage/android/devices'),
   api<{bindings:AndroidBinding[]}>('/manage/android/bindings'),
  ]).then(([inventory,current])=>{
   if(!active)return;
   setDevices(inventory.devices);setBindings(current.bindings);setLoaded(true);
  }).catch(problem=>{if(active)setError(messageFor(problem))}).finally(()=>{if(active)setLoading(false)});
  return()=>{active=false};
 },[revision,refresh]);
 const blocked=loading||saving||!loaded;
 const selectedDevice=devices.find(device=>device.serial===draft.serial);
 const selectedMember=members.find(member=>member.id===draft.memberID);
 const currentBinding=bindings.find(binding=>binding.member_id===draft.memberID);
 const preservingDevice=Boolean(currentBinding?.serial&&draft.serial===currentBinding.serial);
 const canSaveDevice=selectedDevice?.status==='online'||(preservingDevice&&(!selectedDevice||selectedDevice.status==='offline'));
 const memberName=(id:string)=>members.find(member=>member.id===id)?.name??'已不在当前成员列表';
 function selectMember(memberID:string){
  const binding=bindings.find(item=>item.member_id===memberID);
  setDraft({memberID,serial:binding?.serial??'',label:binding?.label??'',packages:binding?.packages.join('\n')??''});
  setNotice('');
 }
 async function save(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();if(blocked)return;
  setError('');setNotice('');
  if(!selectedMember){setError('请明确选择要绑定的家庭成员。');return}
  if(!canSaveDevice){setError('新绑定请选择在线且已完成 USB 调试授权的设备；已有离线设备可以保留原绑定。');return}
  const packages=Array.from(new Set(draft.packages.split(/[\s,，;；]+/).filter(Boolean)));
  if(packages.includes('*')&&packages.length!==1){setError('允许全部应用时只填写 *，不要与具体包名混用。');return}
  if(packages.some(value=>value!=='*'&&!/^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$/.test(value))){
   setError('请每行填写一个真实 Android 包名，或单独填写 * 允许全部应用。');return;
  }
  const label=draft.label.trim();
  if(!label){setError('请填写设备名称。');return}
  setSaving(true);
  try{
   await api('/manage/android/bindings/'+encodeURIComponent(selectedMember.id),'PUT',{serial:draft.serial,label,packages});
   if(!mounted.current)return;
   setDraft(emptyDraft);setNotice(packages.length?'设备绑定和 AI 应用权限已保存。':'设备绑定已保存，AI 不允许操作任何应用；本人仍可接管。');
   setRefresh(value=>value+1);
  }catch(problem){if(mounted.current)setError(messageFor(problem))}
  finally{if(mounted.current)setSaving(false)}
 }
 async function unbind(binding:AndroidBinding){
  if(blocked||!window.confirm('解除“'+memberName(binding.member_id)+'”的安卓设备绑定？该成员将无法继续通过家庭服务操作此设备。'))return;
  setSaving(true);setError('');setNotice('');
  try{
   await api('/manage/android/bindings/'+encodeURIComponent(binding.member_id),'DELETE');
   if(!mounted.current)return;
   setDraft(previous=>previous.memberID===binding.member_id?emptyDraft:previous);
   setNotice('已解除设备绑定。');setRefresh(value=>value+1);
  }catch(problem){if(mounted.current)setError(messageFor(problem))}
  finally{if(mounted.current)setSaving(false)}
 }
 return <section className="settings-form android-workspaces" aria-labelledby="android-workspaces-title">
  <div className="section-title"><h2 id="android-workspaces-title">成员安卓设备</h2><button type="button" disabled={loading||saving} onClick={()=>setRefresh(value=>value+1)}>{loading?'正在检查…':'刷新设备'}</button></div>
  <p>为成员绑定独立的安卓设备，在该成员的客户端查看并接管操作。此处只管理设备绑定，不展示成员的屏幕或账号内容。</p>
  <p className="android-device-note">支持 USB 和局域网无线连接。无线调试先由家庭服务器本机完成配对与登记；切换连接方式不会改变成员绑定。Android 10 需先连接 USB 启用，Android 11 及以上可使用无线调试配对码。</p>
  {error&&<div className="message error" role="alert">{error}</div>}
  {notice&&<div className="message" role="status">{notice}</div>}
  {loading&&<p role="status">正在读取家庭服务器上的安卓设备…</p>}
  {loaded&&<>
   <h3>服务器发现的设备</h3>
   {devices.length?<div className="table-scroll"><table><thead><tr><th scope="col">设备</th><th scope="col">类型</th><th scope="col">连接状态</th></tr></thead><tbody>{devices.map(device=><tr key={device.serial}>
    <td>{device.label||'未命名设备'}<small><code>{device.serial}</code></small></td><td>{backendLabel(device.backend)}</td><td>{statusLabel(device.status)}<small>{transportLabel(device.transport)}{device.wireless_configured?' · 已配置无线':''}</small></td>
   </tr>)}</tbody></table></div>:<p>尚未发现安卓设备。请将专用安卓手机连接到家庭服务器，开启 USB 调试，并在手机上确认该服务器的调试授权，然后刷新。</p>}
   {devices.some(device=>device.status==='unauthorized')&&<p className="android-device-note">存在未授权设备：请在对应安卓手机上确认 USB 调试授权；完成前不能绑定或操作。</p>}
   <h3>当前成员绑定</h3>
   {bindings.length?<div className="table-scroll"><table><thead><tr><th scope="col">成员</th><th scope="col">设备</th><th scope="col">AI 可操作应用</th><th scope="col">操作</th></tr></thead><tbody>{bindings.map(binding=><tr key={binding.member_id}>
    <th scope="row">{memberName(binding.member_id)}</th><td>{binding.label}<small>{backendLabel(binding.backend)} · {statusLabel(binding.status)} · {transportLabel(binding.transport)}</small></td>
    <td>{binding.packages.includes('*')?'全部应用':binding.packages.length?binding.packages.map(name=><code className="android-package" key={name}>{name}</code>):'不允许 AI 操作任何应用'}</td>
    <td><button type="button" disabled={blocked||!members.some(member=>member.id===binding.member_id)} onClick={()=>selectMember(binding.member_id)}>修改绑定</button><button type="button" disabled={blocked} onClick={()=>void unbind(binding)}>解除绑定</button></td>
   </tr>)}</tbody></table></div>:<p>尚未绑定成员。选择成员和设备后保存，不会自动分配。</p>}
  </>}
  <form onSubmit={save}>
   <fieldset disabled={blocked}>
    <legend>{currentBinding?'修改成员设备':'绑定成员设备'}</legend>
    <label>家庭成员<select required value={draft.memberID} onChange={event=>selectMember(event.target.value)}><option value="">请选择成员</option>{members.map(member=><option key={member.id} value={member.id}>{member.name}</option>)}</select></label>
    <label>安卓设备<select required value={draft.serial} onChange={event=>{
     const serial=event.target.value,device=devices.find(item=>item.serial===serial);
     setDraft(previous=>({...previous,serial,label:previous.label||device?.label||''}));
    }}><option value="">请选择设备</option>{draft.serial&&!selectedDevice&&<option value={draft.serial} disabled={!preservingDevice}>保留当前设备（尚未连接）</option>}{devices.map(device=><option key={device.serial} value={device.serial} disabled={device.status!=='online'&&!(device.status==='offline'&&device.serial===currentBinding?.serial)}>{device.label||device.serial} · {backendLabel(device.backend)} · {statusLabel(device.status)}</option>)}</select></label>
    {preservingDevice&&selectedDevice?.status!=='online'&&<small>保留已绑定设备并更新配置不会使其上线；操作前仍需连接设备并完成调试授权。</small>}
    <label>设备名称<input required maxLength={100} value={draft.label} onChange={event=>setDraft(previous=>({...previous,label:event.target.value}))}/></label>
    <label>AI 可操作的应用包名<textarea value={draft.packages} rows={4} maxLength={10000} aria-describedby="android-packages-help" placeholder="每行一个包名，或单独填写 *" autoCapitalize="none" autoCorrect="off" spellCheck={false} onChange={event=>setDraft(previous=>({...previous,packages:event.target.value}))}/></label>
    <small id="android-packages-help">填写 * 允许 AI 操作所有应用；留空禁止 AI 操作任何应用；填写包名只允许对应应用。本人手动接管不受此名单限制，付款等高风险行为仍按任务权限处理。</small>
    <button className="primary" disabled={!selectedMember||!canSaveDevice}>{saving?'正在保存…':'保存绑定'}</button>
   </fieldset>
  </form>
 </section>;
}
