"""使用本地安全权重计算查询和文档相关性，不执行文档内指令。"""
import os
from functools import lru_cache
from pathlib import Path
from fastapi import HTTPException


@lru_cache
def runtime():
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    path = Path(os.environ['RERANKER_MODEL_PATH'])
    tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
    model = AutoModelForSequenceClassification.from_pretrained(
        path, local_files_only=True, trust_remote_code=False, use_safetensors=True)
    # CPU 避免和家庭生成模型争用 Metal 显存，按单请求、两条文档小批次执行。
    torch.set_num_threads(4)
    model.eval()
    return torch, tokenizer, model


def rerank(call):
    query = call.arguments.get('query')
    documents = call.arguments.get('documents')
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
        raise HTTPException(422, '重排查询长度无效')
    if not isinstance(documents, list) or not 1 <= len(documents) <= 20:
        raise HTTPException(422, '重排要求 1 至 20 条文档')
    if any(not isinstance(doc, str) or not 1 <= len(doc) <= 1000 for doc in documents):
        raise HTTPException(422, '重排文档长度无效')
    torch, tokenizer, model = runtime()
    scores = []
    with torch.inference_mode():
        for start in range(0, len(documents), 2):
            pairs = [[query, doc] for doc in documents[start:start + 2]]
            inputs = tokenizer(pairs, padding=True, truncation=True, max_length=512, return_tensors='pt')
            logits = model(**inputs, return_dict=True).logits.reshape(-1).float()
            if not torch.isfinite(logits).all():
                raise HTTPException(502, '重排模型返回非有限分数')
            scores.extend(torch.sigmoid(logits).tolist())
    return {'results': sorted(
        [{'index': index, 'relevance_score': score} for index, score in enumerate(scores)],
        key=lambda item: (-item['relevance_score'], item['index'])),
        'model': 'bge-reranker-base', 'max_pair_tokens': 512}
