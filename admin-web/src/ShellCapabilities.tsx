import {useEffect,useState} from 'react';
import {APIError,api} from './api';

type HostAction={id:string;title:string;description:string;script:string;cwd:string;timeout_seconds:number;fingerprint:string};
type ShellStatus={
 sandbox:{enabled:boolean;ready:boolean;image:string;runtime:string|null;members:string[];reason:string|null};
 host:{enabled:boolean;actions:HostAction[];reason:string|null};
 policy_file:string;
 environment:string;
};

export function ShellCapabilities({revision}:{revision:number}){
 const[status,setStatus]=useState<ShellStatus|null>(null),[busy,setBusy]=useState(true),[error,setError]=useState(''),[refresh,setRefresh]=useState(0);
 useEffect(()=>{
  let active=true;
  setBusy(true);setError('');setStatus(null);
  api<ShellStatus>('/manage/shell').then(value=>{if(active)setStatus(value)}).catch(error=>{
   if(active)setError(error instanceof APIError&&error.status===403?'仅家庭管理员可查看服务端 Shell 配置。':error instanceof Error?error.message:'暂时无法读取 Shell 状态。');
  }).finally(()=>{if(active)setBusy(false)});
  return()=>{active=false};
 },[revision,refresh]);
 return <section className="settings-form" aria-labelledby="shell-capabilities-title">
  <div className="section-title"><h2 id="shell-capabilities-title">服务端 Shell</h2><button disabled={busy} onClick={()=>setRefresh(value=>value+1)}>{busy?'正在检查…':'刷新状态'}</button></div>
  <p>在对话中提出任务，服务器按授权执行命令，并返回输出与生成的文件。手机与浏览器只展示结果。</p>
  {error&&<div className="message error" role="alert">{error}</div>}
  {busy&&<p role="status">正在读取本机执行策略…</p>}
  {status&&<>
   <h3>隔离工作区</h3>
   <div className="row"><b>执行状态</b><span>{!status.sandbox.enabled?'未启用':status.sandbox.ready?'环境检查通过':'当前成员暂不可用'}</span></div>
   {status.sandbox.reason&&<p>{status.sandbox.reason}</p>}
   <p>用于文件处理、计算和运行脚本。工作区仅接收本次任务获准的附件，无外网访问；命令不能直接读取宿主机目录、数据库或其他服务凭据。</p>
   <details><summary>查看运行配置</summary>
    <p>运行环境：{status.environment}</p><p>容器运行时：<code>{status.sandbox.runtime||'Docker 默认'}</code></p>
    <p>环境检查核验当前成员授权，并实际探测固定镜像中的 Shell 与 Python；通过不代表所有任务或生产隔离已经验收。</p>
    <p>固定镜像：<code>{status.sandbox.image||'未配置'}</code></p>
    <p>已授权成员：{status.sandbox.members.length} 位</p>
    {status.sandbox.members.length>0&&<ul>{status.sandbox.members.map(id=><li key={id}><code>{id}</code></li>)}</ul>}
   </details>
   <h3>宿主机管理</h3>
   <div className="row"><b>执行状态</b><span>{status.host.enabled?'已启用':'未启用'}</span></div>
   {status.host.reason&&<p>{status.host.reason}</p>}
   <p>经本机明确授权的家庭管理员可以在对话中调用已登记的管理动作；每次执行前确认具体脚本。动作使用 Core 服务账号的系统权限，不会自动获得 sudo 或管理员权限。脚本与原始输出保存在管理员私有执行报告中，不自动发送给云模型。</p>
   {status.host.actions.length>0?status.host.actions.map(action=><details key={action.id}>
    <summary>{action.title}</summary>
    <p>{action.description}</p>
    <p>动作标识：<code>{action.id}</code> · 最长执行 {action.timeout_seconds} 秒</p>
    <p>工作目录：<code>{action.cwd}</code></p>
    <pre aria-label={`${action.title}的脚本`}>{action.script}</pre>
    <small>配置指纹：<code>{action.fingerprint}</code>。脚本或权限配置改变后，原审批不能继续使用。</small>
   </details>):<p>尚未登记宿主机管理动作。</p>}
   <details><summary>如何启用或调整</summary>
    <p>由服务器本机管理员维护策略文件 <code>{status.policy_file}</code>，分别指定工作区成员、获准宿主管理的家庭管理员和固定动作。文件权限应为 0600，脚本中不要写入密钥。</p>
    <p>此页面只读取配置。登记动作不会自动执行；后续由服务端编排，并在客户端确认。软件安装、服务管理或系统文件修改需要额外系统权限时，由本机管理员单独配置。</p>
    <p>Mac 的 Docker 开发环境与 Linux gVisor 隔离不同，生产环境需完成独立隔离验证。</p>
   </details>
  </>}
 </section>;
}
