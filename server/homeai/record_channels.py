"""统一记录的业务通道；共享底层加密账本不意味着混用数据和记忆接口。"""
from fastapi import HTTPException
from sqlalchemy import and_, or_
from .db import Record, Outbox

MEMORY_SOURCES = {'conversation_memory', 'memory_candidate'}


def is_memory(record):
    return record.kind.startswith('memory.')


def is_business_data(record):
    return not is_memory(record) and not record.kind.startswith('client_input.')


def business_data_clause(model=Record):
    return and_(~model.kind.like('memory.%'), ~model.kind.like('client_input.%'))


def data_event_clause():
    # 老事件缺少分类，仍须传递撤权通知；读取记录时会再按通道过滤。
    return and_(Outbox.kind.like('record.%'), or_(Outbox.record_kind.is_(None),
        and_(~Outbox.record_kind.like('memory.%'), ~Outbox.record_kind.like('client_input.%'))))


def validate_data_upload(item):
    if not is_business_data(item) or item.source in MEMORY_SOURCES:
        raise HTTPException(403, '数据上传不能写入聊天记忆或任务内部记录；请通过聊天和记忆接口处理')
