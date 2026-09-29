import {useEffect,useState} from 'react';
import {api} from './api';

type Frequency='manual'|'daily'|'weekly'|'interval';
type BackupRun={id:string;status:'queued'|'running'|'succeeded'|'failed';requested_at:number;started_at?:number;finished_at?:number;file_name?:string;bytes?:number;error?:string};
type BackupConfiguration={enabled:boolean;directory:string;frequency:Frequency;interval_hours:number;next_run_at:number|null;last_run:BackupRun|null;worker_online:boolean;key_ready:boolean;key_path:string;key_notice:string};
type Archive={name:string;bytes:number;modified_at:number};
const runNames:Record<BackupRun['status'],string>={queued:'等待执行',running:'备份中',succeeded:'备份完成',failed:'备份失败'};
const time=(value:number|null|undefined)=>value?new Date(value*1000).toLocaleString():'—';
const size=(value:number)=>value>=1024*1024?`${(value/1024/1024).toFixed(1)} MB`:`${(value/1024).toFixed(1)} KB`;

export function BackupSettings({archives,onChanged}:{archives:Archive[];onChanged:()=>void}){
 const[configuration,setConfiguration]=useState<BackupConfiguration|null>(null);
 const[enabled,setEnabled]=useState(false),[directory,setDirectory]=useState(''),[frequency,setFrequency]=useState<Frequency>('manual'),[hours,setHours]=useState(24);
 const[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 function apply(value:BackupConfiguration){setConfiguration(value);setEnabled(value.enabled);setDirectory(value.directory);setFrequency(value.frequency);setHours(value.interval_hours)}
 useEffect(()=>{let active=true;api<BackupConfiguration>('/manage/backup-settings').then(value=>{if(active)apply(value)}).catch(e=>{if(active)setError((e as Error).message)});return()=>{active=false}},[]);
 const lastStatus=configuration?.last_run?.status;
 useEffect(()=>{
  if(lastStatus!=='queued'&&lastStatus!=='running')return;
  let active=true;
  const timer=setInterval(()=>{api<BackupConfiguration>('/manage/backup-settings').then(value=>{if(!active)return;setConfiguration(value);if(value.last_run?.status==='succeeded'||value.last_run?.status==='failed')onChanged()}).catch(e=>{if(active)setError((e as Error).message)})},5000);
  return()=>{active=false;clearInterval(timer)};
 },[lastStatus]);
 async function save(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();setBusy(true);setError('');setNotice('');
  try{const value=await api<BackupConfiguration>('/manage/backup-settings','PUT',{enabled:enabled&&frequency!=='manual',directory:directory.trim(),frequency,interval_hours:hours});apply(value);setNotice(value.enabled?'设置已保存，将按所选频率执行备份。':'设置已保存，自动备份保持关闭。');onChanged()}
  catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 async function run(){
  setBusy(true);setError('');setNotice('');
  try{const value=await api<BackupConfiguration>('/manage/backup-settings/run','POST');setConfiguration(value);setNotice('备份请求已提交，由服务器执行。');onChanged()}
  catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 const dirty=configuration&&(enabled!==configuration.enabled||directory.trim()!==configuration.directory||frequency!==configuration.frequency||hours!==configuration.interval_hours);
 return <><section className="settings-form"><h2>备份设置</h2><p>默认关闭。先选择家庭服务器上的保存位置与频率，再保存设置。</p>
  {error&&<div className="message error" role="alert">{error}</div>}{notice&&<div className="message" role="status">{notice}</div>}
  {!configuration?<p>{error?'暂时无法读取配置，请刷新页面重试。':'正在读取备份配置…'}</p>:<>
   <div className="row"><span>自动备份</span><b>{configuration.enabled?'已启用':'已关闭'}</b></div><div className="row"><span>备份进程</span><span>{configuration.worker_online?'在线':'未运行'}</span></div>
   <form onSubmit={save}><label>备份保存位置<input value={directory} onChange={e=>setDirectory(e.target.value)} placeholder="服务器上的绝对目录路径" autoComplete="off" disabled={busy} required={enabled}/><small>填写运行家庭服务的电脑目录，不是当前浏览器或手机的目录。</small></label>
    <label>备份频率<select value={frequency} onChange={e=>{const value=e.target.value as Frequency;setFrequency(value);if(value==='manual')setEnabled(false)}} disabled={busy}><option value="manual">仅手动</option><option value="daily">每天一次</option><option value="weekly">每周一次</option><option value="interval">自定义间隔</option></select></label>
    {frequency==='interval'&&<label>间隔（小时）<input type="number" min={1} max={8760} step={1} value={hours} onChange={e=>setHours(Number(e.target.value))} required disabled={busy}/></label>}
    {frequency!=='manual'&&<label className="settings-toggle"><input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)} disabled={busy}/>启用定时备份</label>}
    <button className="primary" disabled={busy}>{busy?'正在保存…':'保存备份设置'}</button>
   </form>
   <div className="row"><span>下次计划</span><span>{configuration.enabled?time(configuration.next_run_at):'未启用'}</span></div>
   <div className="row"><span>最近执行</span><span>{configuration.last_run?`${runNames[configuration.last_run.status]} · ${time(configuration.last_run.finished_at??configuration.last_run.started_at??configuration.last_run.requested_at)}`:'尚未执行'}</span></div>
   {configuration.last_run?.error&&<p role="alert">{configuration.last_run.error}</p>}
   {configuration.last_run?.file_name&&<p>{configuration.last_run.file_name}{configuration.last_run.bytes!==undefined?` · ${size(configuration.last_run.bytes)}`:''}</p>}
   <button disabled={busy||Boolean(dirty)||!configuration.directory||lastStatus==='queued'||lastStatus==='running'} onClick={run}>立即备份</button>
   {dirty&&<small>先保存修改，再按已保存的位置执行。</small>}
   <details><summary>密钥与恢复</summary><p>{configuration.key_notice}</p>{configuration.key_path&&<p>备份密钥位置：<code>{configuration.key_path}</code>（{configuration.key_ready?'已准备':'尚未准备'}）</p>}<p>恢复需要备份密钥、数据主密钥和加密归档，请在家庭服务器本机操作。关闭自动备份不删除已有文件。</p><p>服务端启动命令：<code>PYTHONPATH=server .venv/bin/python scripts/run_backup_worker.py</code></p></details>
  </>}
 </section><section><h2>已保存的备份</h2>{archives.length?<div className="table-scroll"><table><thead><tr><th>文件</th><th>大小</th><th>更新时间</th></tr></thead><tbody>{archives.map(archive=><tr key={archive.name}><td>{archive.name}</td><td>{size(archive.bytes)}</td><td>{time(archive.modified_at)}</td></tr>)}</tbody></table></div>:<p>当前保存位置暂无备份。</p>}</section></>;
}
