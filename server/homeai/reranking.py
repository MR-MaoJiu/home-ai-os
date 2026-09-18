"""重排器仅返回候选序号与分数，不能注入额外文档或替换规范内容。"""
import math
from fastapi import HTTPException


def ordered_indices(result, count):
    rows = result.get('results') if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) != count:
        raise HTTPException(502, '重排结果数量不符')
    scores = {}
    for row in rows:
        if not isinstance(row, dict):
            raise HTTPException(502, '重排结果格式无效')
        index, score = row.get('index'), row.get('relevance_score')
        if type(index) is not int or not 0 <= index < count or index in scores:
            raise HTTPException(502, '重排候选序号无效')
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise HTTPException(502, '重排分数无效')
        scores[index] = score
    return sorted(scores, key=lambda index: (-scores[index], index))
