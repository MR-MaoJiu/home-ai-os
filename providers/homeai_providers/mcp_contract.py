"""MCP 工具目录只作为不可信协议数据；目录改变必须重新审核。"""
import hashlib
import json
from fastapi import HTTPException


async def catalog(session):
    tools, cursor, seen = [], None, set()
    for _ in range(20):
        page = await session.list_tools(cursor=cursor)
        tools.extend(tool.model_dump(mode='json', exclude_none=True) for tool in page.tools)
        if len(tools) > 200:
            raise HTTPException(502, 'MCP 工具目录超过限制')
        cursor = page.nextCursor
        if not cursor:
            break
        if cursor in seen:
            raise HTTPException(502, 'MCP 工具目录游标没有前进')
        seen.add(cursor)
    else:
        raise HTTPException(502, 'MCP 工具目录分页过多')
    names = [tool['name'] for tool in tools]
    if len(names) != len(set(names)):
        raise HTTPException(502, 'MCP 工具目录包含重名')
    encoded = json.dumps(sorted(tools, key=lambda value: value['name']), sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    if len(encoded) > 1024 * 1024:
        raise HTTPException(502, 'MCP 工具目录过大')
    return hashlib.sha256(encoded).hexdigest(), tools


async def checked_call(session, tool, arguments, expected_hash, structured=False):
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise HTTPException(403, 'MCP 尚未配置审核过的工具目录指纹')
    actual, tools = await catalog(session)
    if actual != expected_hash:
        raise HTTPException(409, 'MCP 工具目录已变化，必须重新审核映射')
    if tool not in {value['name'] for value in tools}:
        raise HTTPException(403, '映射工具不在已审核目录中')
    result = await session.call_tool(tool, arguments)
    if result.isError:
        raise HTTPException(502, 'MCP 工具执行失败')
    value = result.model_dump(mode='json')
    if len(json.dumps(value).encode()) > 4 * 1024 * 1024:
        raise HTTPException(502, 'MCP 工具结果过大')
    if structured:
        if not isinstance(result.structuredContent, dict):
            raise HTTPException(502, "映射到 Core 的 MCP 工具必须返回结构化对象")
        return result.structuredContent
    return value


from contextlib import asynccontextmanager

@asynccontextmanager
async def protocol_errors():
    try:
        yield
    except BaseExceptionGroup as group:
        def leaves(error):
            return [item for child in error.exceptions for item in leaves(child)] if isinstance(error, BaseExceptionGroup) else [error]
        errors = leaves(group)
        if len(errors) == 1 and isinstance(errors[0], HTTPException):
            raise errors[0] from None
        raise
