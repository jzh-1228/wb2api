"""后台「设置 → 运行配置」的读写接口（require_admin）。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin.db import get_db
from admin.runtime_settings import save, snapshot
from admin.security import require_admin

router = APIRouter(prefix="/api/runtime-config", tags=["runtime-config"],
                   dependencies=[Depends(require_admin)])


class UpdateIn(BaseModel):
    updates: dict[str, object]


@router.get("")
def read_config():
    return {"fields": snapshot()}


@router.put("")
def write_config(payload: UpdateIn, db: Session = Depends(get_db)):
    result = save(db, payload.updates)
    result["fields"] = snapshot()
    return result
