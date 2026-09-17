"""固定条件运算，无 eval、脚本或模型判断。条件只观察已授权的持久步骤结果。"""
import math
from fastapi import HTTPException

MISSING = object()


def evaluate(condition, completed, vault, user_id):
    cache = {}

    def matches(predicate):
        index = predicate['step']
        if index not in cache:
            row = completed.get(index)
            cache[index] = vault.open(row.result, user_id + ':invocation-result:' + row.id) if row and row.result else MISSING
        value = cache[index]
        for key in predicate.get('path', []):
            if isinstance(value, dict) and isinstance(key, str):
                value = value.get(key, MISSING)
            elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
                value = value[key]
            else:
                value = MISSING
                break
        operator = predicate['operator']
        if operator == 'exists':
            return value is not MISSING
        if operator == 'not_exists':
            return value is MISSING
        if value is MISSING:
            raise HTTPException(422, '条件引用的前序结果或路径不存在，请用 exists/not_exists 显式处理')
        if operator == 'not_empty':
            if not isinstance(value, (str, list, dict)):
                raise HTTPException(422, 'not_empty 只接受文本、数组或对象')
            return len(value) > 0
        expected = predicate.get('value')
        if operator in {'equals', 'not_equals'}:
            # JSON 布尔值不是数字，防止 Python 将 true 与 1 判断为相等。
            numeric = type(value) in {int, float} and type(expected) in {int, float}
            equal = (numeric or type(value) is type(expected)) and value == expected
            return equal if operator == 'equals' else not equal
        if type(value) not in {int, float} or (isinstance(value, float) and not math.isfinite(value)):
            raise HTTPException(422, '条件大小比较的来源必须为有限数值')
        if operator == 'gt':
            return value > expected
        if operator == 'gte':
            return value >= expected
        if operator == 'lt':
            return value < expected
        if operator == 'lte':
            return value <= expected
        raise HTTPException(422, '未知条件运算')

    results = (matches(predicate) for predicate in condition['predicates'])
    return all(results) if condition['mode'] == 'all' else any(results)
