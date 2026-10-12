"""额度的不限状态显式表示；数值范围仅用于 JSON 整数精度，不充当不限标记。"""
from typing import Annotated, Literal
from pydantic import Field, StrictInt

UNLIMITED_TOKENS = 'unlimited'
MAX_FINITE_TOKENS = 9007199254740991
TokenLimit = Annotated[StrictInt, Field(ge=0, le=MAX_FINITE_TOKENS)] | Literal['unlimited']


def token_remaining(limit, used):
    return None if limit == UNLIMITED_TOKENS else max(0, limit-used)


def shared_remaining(member_limit, member_used, household_limit, household_used):
    values = [value for value in (token_remaining(member_limit,member_used), token_remaining(household_limit,household_used)) if value is not None]
    return min(values) if values else None
