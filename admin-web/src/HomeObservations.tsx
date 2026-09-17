import {useEffect,useState} from 'react';
import {api} from './api';
type Observation={provider_id:string;status:string;error_type:string|null;states:{revision:number;observed_at:number;state:{entity_id:string;state?:string;missing?:boolean;attributes?:{friendly_name?:string}}}[]};
export function HomeObservations(){
 const[items,setItems]=useState<Observation[]>([]),[error,setError]=useState(''),[loaded,setLoaded]=useState(false);
 useEffect(()=>{
  let active=true;let timer:ReturnType<typeof setTimeout>|undefined;
  async function refresh(){
   try{const value=await api<Observation[]>('/home/observations');if(active){setItems(value);setError('');setLoaded(true)}}
   catch(error){if(active){setItems([]);setError(error instanceof Error?error.message:'家居观察暂不可用');setLoaded(true)}}
   finally{if(active)timer=setTimeout(refresh,5000)}
  }
  void refresh();return()=>{active=false;if(timer)clearTimeout(timer)};
 },[]);
 return <section><h2>家居实时观察</h2><p>仅展示明确授权的实体。断线后保留最后观察值，重新连接会读取当前快照，不补造历史事件。</p>
  {error&&<p role="alert">{error}</p>}
  {!loaded?<p>正在读取…</p>:!items.length&&!error?<p>尚未启用家居事件观察，请由管理员配置实体授权与订阅。</p>:null}
  {items.map(item=><div key={item.provider_id}><h3>{item.provider_id}</h3><p>{item.status==='CONNECTED'?'订阅已连接':item.status==='CONNECTING'?'正在连接':'连接已断开，以下可能是旧状态'}{item.error_type?' · '+item.error_type:''}</p>
   {item.states.map(row=><div className="row" key={row.state.entity_id}><b>{row.state.attributes?.friendly_name??row.state.entity_id}</b><span>{row.state.missing?'源实体不存在':row.state.state}</span><time>{new Date(row.observed_at*1000).toLocaleString()}</time></div>)}
  </div>)}
 </section>;
}
