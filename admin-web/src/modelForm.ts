// Provider 标识统一为小写；模型名称保留服务商要求的大小写。
export function modelConfiguration(form: FormData) {
 const id=String(form.get('id')??'').trim().toLowerCase();
 if(!/^[a-z][a-z0-9_.-]{1,80}$/.test(id))throw new Error('Provider ID 需为 2–81 位，以英文字母开头，仅含英文、数字、点、下划线或连字符。');
 const endpoint=String(form.get('endpoint')??'').trim().replace(/\/+$/,'');
 let url:URL;
 try{url=new URL(endpoint)}catch{throw new Error('接口地址无效，请填写完整的 HTTP(S) Base URL。')}
 if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash)throw new Error('接口地址必须为不含账号、密码、查询参数或片段的 HTTP(S) 地址。');
 const cloud=form.get('type')==='cloud';
 if(cloud&&url.protocol!=='https:')throw new Error('云端模型需要 HTTPS 接口地址。');
 const model=String(form.get('model')??'').trim();
 if(!model)throw new Error('请填写服务商提供的模型名称。');
 return {id,version:'1.0.0',adapter:'openai',endpoint,model,cloud,capabilities:{'model.generate@v1':'chat'},allowed_hosts:[url.hostname]};
}
