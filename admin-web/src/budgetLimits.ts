// 这是 JavaScript 整数精度边界，不是业务额度上限；不限使用显式字符串。
export const MAX_FINITE_TOKEN_LIMIT=Number.MAX_SAFE_INTEGER;
export type TokenLimit=number|'unlimited';

export function parseTokenLimit(value:string):TokenLimit{
 if(value==='unlimited')return 'unlimited';
 const parsed=Number(value);
 if(!/^\d+$/.test(value)||!Number.isSafeInteger(parsed)||parsed<0||parsed>MAX_FINITE_TOKEN_LIMIT)throw new Error('有限 Token 额度需填写非负安全整数；取消额度上限请选择“不限”。');
 return parsed;
}

export function formatTokenLimit(value:TokenLimit|null):string{
 return value===null||value==='unlimited'?'不限':value.toLocaleString('zh-CN');
}
