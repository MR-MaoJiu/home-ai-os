import {useEffect,useRef,useState} from 'react';
import {api} from './api';

export function Speech(){
 const[text,setText]=useState(''),[voices,setVoices]=useState<string[]>([]),[speaker,setSpeaker]=useState('');
 const[busy,setBusy]=useState(false),[status,setStatus]=useState(''),[error,setError]=useState(''),[url,setUrl]=useState('');
 const generation=useRef(0),pending=useRef<string|null>(null),objectURL=useRef(''),audio=useRef<HTMLAudioElement>(null);
 function clearAudio(){audio.current?.pause();if(objectURL.current)URL.revokeObjectURL(objectURL.current);objectURL.current='';setUrl('')}
 function stop(){generation.current++;pending.current=null;clearAudio();setBusy(false)}
 useEffect(()=>{
  const hide=()=>{if(document.hidden)stop()};document.addEventListener('visibilitychange',hide);
  return()=>{generation.current++;audio.current?.pause();if(objectURL.current)URL.revokeObjectURL(objectURL.current);document.removeEventListener('visibilitychange',hide)};
 },[]);
 async function submit(capability:string){
  if(busy)return;clearAudio();setBusy(true);setError('');const run=++generation.current;
  try{
   const task=await api<{id:string}>('/tasks','POST',{idempotency_key:crypto.randomUUID(),capability,step_timeout_seconds:300,arguments:capability==='speech.voices@v1'?{}:{text,speaker}});
   if(generation.current!==run)return;pending.current=task.id;
   const deadline=Date.now()+310000;
   while(Date.now()<deadline){
    if(generation.current!==run)return;
    const taskResult=await api<{status:string,error?:string,result?:{voices?:string[],format?:string,audio_base64?:string}}>('/tasks/'+task.id);
    if(generation.current!==run)return;setStatus('服务器任务：'+taskResult.status);
    if(taskResult.status==='SUCCEEDED'){
     pending.current=null;const result=taskResult.result;
     if(capability==='speech.voices@v1'){
      if(!Array.isArray(result?.voices)||!result.voices.length||result.voices.length>50||result.voices.some(v=>typeof v!=='string'||!v||v.length>100))throw Error('音色列表格式无效');
      setVoices(result.voices);setSpeaker(result.voices.includes('中文女')?'中文女':result.voices[0]);setStatus('音色已加载');
     }else{
      if(result?.format!=='wav'||typeof result.audio_base64!=='string'||result.audio_base64.length>3000000)throw Error('音频格式无效');
      const bytes=Uint8Array.from(atob(result.audio_base64),c=>c.charCodeAt(0));
      if(bytes.length<44||String.fromCharCode(...bytes.slice(0,4))!=='RIFF'||String.fromCharCode(...bytes.slice(8,12))!=='WAVE')throw Error('WAV 音频无效');
      objectURL.current=URL.createObjectURL(new Blob([bytes],{type:'audio/wav'}));setUrl(objectURL.current);setStatus('合成完成，请点击播放');
     }
     return;
    }
    if(['FAILED','CANCELED','AWAITING_APPROVAL','NEEDS_RECONCILIATION'].includes(taskResult.status))throw Error(taskResult.error??taskResult.status);
    await new Promise(resolve=>setTimeout(resolve,1000));
   }
   throw Error('任务仍在服务器运行，请在任务与审批中查看；没有重新提交');
  }catch(error){if(generation.current===run)setError(error instanceof Error?error.message:'语音操作失败')}
  finally{if(generation.current===run)setBusy(false)}
 }
 async function cancel(){const task=pending.current;stop();setStatus('已停止等待与播放');if(task){try{await api('/tasks/'+task+'/cancel','POST')}catch{setError('取消未确认，请在任务与审批中核查')}}}
 return <section className="speech"><h2>语音朗读</h2><p>使用家庭服务器内置音色。音频只保留在当前页面，切换页面或进入后台会停止播放。</p>
  <label className="field"><span>朗读文本</span><textarea value={text} onChange={event=>setText(event.target.value)} disabled={busy} rows={4}/></label><small>{Array.from(text).length} / 300 字符</small>
  <div className="inline"><button disabled={busy} onClick={()=>submit('speech.voices@v1')}>获取音色</button>
   <label>音色<select value={speaker} disabled={busy||!voices.length} onChange={event=>setSpeaker(event.target.value)}>{!voices.length&&<option value="">尚未加载</option>}{voices.map(voice=><option key={voice}>{voice}</option>)}</select></label>
   <button className="primary" disabled={busy||!speaker||!text.trim()||Array.from(text).length>300} onClick={()=>submit('speech.synthesize@v1')}>合成语音</button>
   {(busy||url)&&<button onClick={cancel}>停止</button>}</div>
  {url&&<audio ref={audio} controls src={url} onError={()=>{clearAudio();setError('浏览器无法播放此音频')}}/>}
  {status&&<p role="status">{status}</p>}{error&&<p role="alert">{error}</p>}
 </section>;
}
