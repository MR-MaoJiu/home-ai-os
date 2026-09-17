"""为已验证本地推理版本展开 JSON Schema 引用，不转换或伪造模型输出。"""
from copy import deepcopy


def inline_schema(schema):
    root=deepcopy(schema)
    def expand(value, stack=(), depth=0):
        if depth>64:raise ValueError('Schema 嵌套过深')
        if isinstance(value,list):return [expand(item,stack,depth+1) for item in value]
        if not isinstance(value,dict):return value
        value=dict(value)
        if '$ref' in value:
            ref=value.pop('$ref')
            if not isinstance(ref,str) or not ref.startswith('#/') or ref in stack:
                raise ValueError('不允许远程或循环 Schema 引用')
            target=root
            for part in ref[2:].split('/'):
                target=target[part.replace('~1','/').replace('~0','~')]
            merged={**expand(target,stack+(ref,),depth+1),**value}
            return expand(merged,stack,depth+1)
        result={key:expand(item,stack,depth+1) for key,item in value.items() if key!='$defs'}
        if result.get('type')=='object' and 'properties' in result:
            result.setdefault('additionalProperties',False)
        return result
    return expand(root)
