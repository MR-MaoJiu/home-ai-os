import {useEffect,useState} from 'react';
import {api} from './api';

type NotificationConfiguration={
 enabled:boolean;key_id:string;team_id:string;topic:string;private_key_configured:boolean;
 push_configured:boolean;worker_online:boolean;
 status:'not_configured'|'disabled'|'invalid_configuration'|'worker_offline'|'ready';
 source:'managed'|'environment'|'none';validation:'structure_only';
};
const statusNames:Record<NotificationConfiguration['status'],string>={
 not_configured:'尚未配置',disabled:'已关闭系统推送',invalid_configuration:'配置不完整或格式无效',
 worker_offline:'通知进程未运行',ready:'已配置，待实际投递验证',
};

export function NotificationSettings(){
 const[configuration,setConfiguration]=useState<NotificationConfiguration|null>(null);
 const[enabled,setEnabled]=useState(false),[keyID,setKeyID]=useState(''),[teamID,setTeamID]=useState(''),[topic,setTopic]=useState('');
 const[privateKey,setPrivateKey]=useState(''),[fileRevision,setFileRevision]=useState(0);
 const[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 function apply(value:NotificationConfiguration){setConfiguration(value);setEnabled(value.enabled);setKeyID(value.key_id);setTeamID(value.team_id);setTopic(value.topic)}
 useEffect(()=>{let active=true;api<NotificationConfiguration>('/manage/notifications').then(value=>{if(active)apply(value)}).catch(e=>{if(active)setError((e as Error).message)});return()=>{active=false}},[]);
 async function save(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();setBusy(true);setError('');setNotice('');
  try{
   const value=await api<NotificationConfiguration>('/manage/notifications','PUT',{enabled,key_id:keyID.trim(),team_id:teamID.trim(),topic:topic.trim(),...(privateKey?{private_key:privateKey}:{})});
   apply(value);setPrivateKey('');setFileRevision(v=>v+1);setNotice(enabled?'配置已保存，通知进程会使用新配置。请在真实设备上验证投递。':'系统推送已关闭，站内通知仍保留。');
  }catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 return <section className="settings-form"><h2>iOS 系统通知</h2><p>服务器执行任务后发送通知。手机收到提示，再从家庭服务器读取详情。</p>
  {error&&<div className="message error" role="alert">{error}</div>}{notice&&<div className="message" role="status">{notice}</div>}
  {!configuration?<p>{error?'暂时无法读取配置，请刷新页面重试。':'正在读取通知配置…'}</p>:<><div className="row"><span>系统推送</span><b>{statusNames[configuration.status]}</b></div><div className="row"><span>通知进程</span><span>{configuration.worker_online?'在线':'未运行'}</span></div><div className="row"><span>APNs 私钥</span><span>{configuration.private_key_configured?'已配置，不回显':'未配置'}</span></div>
   <form onSubmit={save}><label className="settings-toggle"><input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)} disabled={busy}/>启用 iOS 系统推送</label>
    <label>Key ID<input value={keyID} onChange={e=>setKeyID(e.target.value)} required={enabled} pattern="[A-Z0-9]{10}" maxLength={10} autoComplete="off" disabled={busy}/></label>
    <label>Team ID<input value={teamID} onChange={e=>setTeamID(e.target.value)} required={enabled} pattern="[A-Z0-9]{10}" maxLength={10} autoComplete="off" disabled={busy}/></label>
    <label>App Bundle ID<input value={topic} onChange={e=>setTopic(e.target.value)} required={enabled} maxLength={255} autoComplete="off" disabled={busy}/></label>
    <label>APNs 私钥文件（.p8）<input key={fileRevision} type="file" accept=".p8" disabled={busy} onChange={async e=>{const file=e.target.files?.[0];setPrivateKey('');setError('');if(!file)return;if(file.size>10000){setError('私钥文件不能超过 10 KB');return}setBusy(true);try{setPrivateKey(await file.text())}catch{setError('无法读取私钥文件，请重新选择')}finally{setBusy(false)}}}/><small>{privateKey?'已读取所选文件，保存后加密存储在家庭服务器。':'已有私钥时留空会保留原密钥；私钥保存后不回显。'}</small></label>
    <button className="primary" disabled={busy}>{busy?'正在保存…':'保存通知配置'}</button>
   </form><p>状态只校验配置格式与进程在线，不代表 Apple 已接受或手机已收到通知。关闭推送不会删除通知记录或已有密钥。</p>
   <details><summary>自托管与官方应用</summary><p>自编译应用使用自己的 Apple Developer Team、APNs Key 和 Bundle ID。官方发行应用的私钥不能分发给家庭服务器；官方推送代理尚未接入。iOS 还需允许通知，并使用含推送权限的签名。</p><p>服务端启动命令：<code>PYTHONPATH=server .venv/bin/python scripts/run_notification_worker.py</code></p></details>
  </>}
 </section>;
}
