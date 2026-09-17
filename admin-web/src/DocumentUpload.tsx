import {useState} from 'react';
import {api,uploadDocument} from './api';

export function DocumentUpload({onChanged}:{onChanged:()=>void}){
 const[file,setFile]=useState<File|null>(null),[busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[error,setError]=useState('');
 async function submit(event:React.FormEvent){
  event.preventDefault();if(!file)return;
  setError('');setNotice('');setBusy(true);
  try{
   if(file.size>20*1024*1024)throw new Error('附件不能超过 20 MB');
   const record=await uploadDocument(file);
   const task=await api('/files/'+record.id+'/parse','POST');
   setNotice('附件已加密保存，解析任务 '+task.id.slice(0,8)+' 已提交。请在“任务与审批”查看结果；完成后刷新数据列表。');
   onChanged();
  }catch(e){setError(e instanceof Error?e.message:'文档提交失败')}
  finally{setBusy(false)}
 }
 return <section><h2>导入文档</h2><p>正文由本地 Docling 解析。请先为当前成员配置解析 Provider；未配置时任务会明确失败，不生成示例正文。</p>
  <form onSubmit={submit}><label>选择文件<input type="file" accept=".pdf,.docx,.pptx,.html,.txt,.md,.png,.jpg" disabled={busy} onChange={e=>setFile(e.target.files?.[0]??null)}/></label><button className="primary" disabled={busy||!file}>{busy?'正在提交…':'上传并解析'}</button></form>
  {notice&&<p className="message" role="status">{notice}</p>}{error&&<p className="message error" role="alert">{error}</p>}
 </section>
}
