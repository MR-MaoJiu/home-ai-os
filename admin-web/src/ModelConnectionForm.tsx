import {useState} from 'react';
import {api} from './api';
import {modelConfiguration} from './modelForm';

export function ModelConnectionForm({onChanged,notify}:{onChanged:()=>void;notify:(text:string)=>void}){
 const[busy,setBusy]=useState(false),[error,setError]=useState('');
 async function save(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();if(busy)return;
  const form=new FormData(event.currentTarget);setBusy(true);setError('');
  try{
   const manifest=modelConfiguration(form),path='/providers/'+encodeURIComponent(manifest.id);
   await api(path,'PUT',manifest);
   const key=String(form.get('key')??'').trim();
   if(key){const secret=await api<{id:string}>('/secrets','POST',{provider_id:manifest.id,value:key});await api(path,'PUT',{...manifest,secret_id:secret.id})}
   notify('配置已保存，默认停用。请在 Provider 中启用，再选择对应模型用途；图片请求还需设置每日张数额度。');onChanged();
  }catch(problem){setError((problem as Error).message)}finally{setBusy(false)}
 }
 return <div className="columns"><section className="settings-form"><h2>接入模型</h2>
  {error&&<div className="message error" role="alert">{error}</div>}
  <form onSubmit={save}>
   <label>Provider ID<input name="id" placeholder="例如：cloud.images" required minLength={2} maxLength={81} pattern={'[A-Za-z][A-Za-z0-9_.\\-]{1,80}'} disabled={busy}/><small>英文大小写均可，保存时统一为小写；可使用数字、点、下划线或连字符。</small></label>
   <label>模型接口类型<select name="interface" defaultValue="chat" disabled={busy}><option value="chat">对话 / 图片理解（Chat Completions）</option><option value="image_generation">图片生成（Images Generations）</option><option value="image_editing">图片编辑（Images Edits）</option><option value="image_both">图片生成与编辑（Images API）</option></select><small>图片理解分析已有图片；生成和编辑使用独立的 Images API。仅勾选服务商确实支持的接口。</small></label>
   <label>接口地址<input name="endpoint" type="url" placeholder="https://服务地址/v1" required disabled={busy}/><small>填写兼容 OpenAI 接口的 Base URL，不包含 /chat/completions、/images/generations 或 /images/edits。本地可使用 localhost 或局域网 IP。</small></label>
   <label>模型名称<input name="model" placeholder="填写该接口实际支持的模型名称" required disabled={busy}/></label>
   <div role="group" aria-label="部署类型">部署类型<div className="radios"><label><input name="type" type="radio" value="local" defaultChecked disabled={busy}/>本地</label><label><input name="type" type="radio" value="cloud" disabled={busy}/>云端</label></div></div>
   <label>API Key<input name="key" type="password" autoComplete="new-password" placeholder="本地无密钥服务可留空" disabled={busy}/><small>凭据加密保存在家庭服务器，不会回显。</small></label>
   <button className="primary" disabled={busy}>{busy?'正在保存…':'保存配置'}</button>
  </form>
 </section><section className="help"><h2>配置说明</h2><h3>按用途分别配置</h3><p>对话、图片理解、图片生成、图片编辑可以选择不同的模型。配置图片接口不会自动开通服务商不支持的能力。</p><hr/><h3>额度与验证</h3><p>图片生成与编辑共用单独的每日张数额度，默认 0 张。配置用途、启用 Provider 并设置额度后才可请求；尚未实测会保持“未验证”。</p><hr/><h3>凭据与隐私</h3><p>配置云服务不等于授权个人数据上云。图片编辑的原图与提示仍由服务端检查授权，模型密钥不下发给手机。</p></section></div>;
}
