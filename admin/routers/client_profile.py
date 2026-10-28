#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端参数档案的 HTTP 接口（探测 / 查看 / 保存 / 重置）。

背景：出站请求要伪装成官方桌面端，涉及 UA、版本号、风控头、桌面指纹等一批
参数。这些值需要「本地探测 → 后台保存 → 线上同步」这条链路，所以给它们
一套 REST 接口（见 `admin/client_profile.py` 的说明）。

接口一览::

    GET  /api/client-profile              当前生效值 + 每项来源 + 已保存值
    POST /api/client-profile/detect       现场探测本机客户端（不保存）
    PUT  /api/client-profile              保存（merge 语义，可只改一个字段）
    POST /api/client-profile/reset        清空保存值，回到纯探测

安全：全部需要管理员鉴权（`require_admin`）。档案本身不含凭据，但
「伪装参数」被随意改动会直接影响线上稳定性，所以不对外开放。
"""
from __future__ import annotations

import logging
import json

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from admin import client_profile as cprofile
from admin.db import get_db
from admin.security import require_admin

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/client-profile", tags=["client-profile"])
PORTABLE_FORMAT = "workbuddy-client-profile"


class SaveIn(BaseModel):
    """保存请求。

    profile 只需给出要改的字段（merge 语义）：比如只改 os_version，
    其余保持不动。merge=False 则整体替换。
    """

    profile: dict = Field(default_factory=dict)
    source: str | None = Field(
        default=None,
        description="auto=优先现场探测 / saved=钉住已保存值（线上实例应选 saved）")
    merge: bool = True


def _payload(db: Session) -> dict:
    """组装给前端的完整视图。"""
    eff = cprofile.effective(db)
    sav = cprofile.saved()
    detail = cprofile.snapshot_detail()
    det = detail.get("values") or {}
    return {
        "effective": eff,
        "saved": sav,
        "detected": det,
        # 每个探测键的来源：安装包（product.json 等）或本机系统（OS/硬件/时区）
        "detected_origins": detail.get("origins") or {},
        "detected_package": detail.get("package") or {},
        "detected_system": detail.get("system") or {},
        "sources": cprofile.sources(db),
        "source": cprofile.source(),
        "defaults": cprofile.DEFAULTS,
        # 有探测值 = 这台机器上确实装了客户端（线上通常为空）
        "has_client": bool(det),
        "diff_saved_vs_effective": cprofile.diff(sav, eff),
        "runtime_diagnostics": cprofile.runtime_diagnostics(),
    }


@router.get("")
def get_profile(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    return _payload(db)


@router.post("/detect")
def detect(_: bool = Depends(require_admin)):
    """现场探测本机客户端（只读，不写库）。

    返回安装包 + 本机系统两类只读值，并按 origin 标明每个值的出处；
    线上服务器上探测结果通常为空 —— 这正是需要「本地探测后同步过去」的原因。
    """
    detail = cprofile.snapshot_detail()
    det = detail.get("values") or {}
    return {
        "ok": True,
        "detected": det,
        "detected_origins": detail.get("origins") or {},
        "detected_package": detail.get("package") or {},
        "detected_system": detail.get("system") or {},
        "has_client": bool(det),
        "runtime_diagnostics": cprofile.runtime_diagnostics(),
        "message": ("探测到本机客户端" if det else
                    "未探测到本机 WorkBuddy 桌面端（线上实例属正常；"
                    "请在装了客户端的机器上探测后同步过来）"),
    }


@router.put("")
def save(body: SaveIn, _: bool = Depends(require_admin),
         db: Session = Depends(get_db)):
    # profile 可以为空 —— 只切换取值策略（source）也是合法操作：
    # 「钉住已保存值」与「优先现场探测」的切换不涉及任何字段改动。
    if not isinstance(body.profile, dict):
        raise HTTPException(status_code=400, detail="profile 必须是对象")
    if not body.profile and body.source is None:
        raise HTTPException(status_code=400,
                            detail="profile 与 source 不能同时为空")
    if body.source is not None and body.source not in cprofile._VALID_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"source 只能是 {list(cprofile._VALID_SOURCES)}")
    res = cprofile.save(body.profile, new_source=body.source, db=db,
                        merge=body.merge)
    if not res.get("ok"):
        raise HTTPException(status_code=400,
                            detail="；".join(res.get("errors") or ["保存失败"]))
    _logger.info("客户端参数档案已更新（source=%s，改动=%s）",
                 res.get("source"), sorted((body.profile or {}).keys()))
    return {"ok": True, "errors": res.get("errors") or [],
            "source": res.get("source"), **_payload(db)}


@router.post("/reset")
def reset(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    cprofile.reset(db)
    return {"ok": True, **_payload(db)}


@router.get("/export")
def export_profile(response: Response, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    profile = dict(cprofile.effective(db))
    profile.pop("user_agent", None)  # Derived again from the imported versions.
    clean, errors = cprofile.normalize(profile)
    if errors:
        raise HTTPException(400, detail="；".join(errors))
    response.headers['Cache-Control'] = 'no-store'
    return {"format": PORTABLE_FORMAT, "version": 1, "source": "saved", "profile": clean}


@router.post("/import")
def import_profile(body: dict, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    if body.get("format") != PORTABLE_FORMAT or type(body.get("version")) is not int or body.get("version") != 1:
        raise HTTPException(400, detail="请选择客户端参数导出的 JSON 文件（格式版本 1）")
    if len(json.dumps(body, ensure_ascii=False).encode('utf-8')) > 256 * 1024:
        raise HTTPException(400, detail="客户端参数文件不能超过 256 KB")
    clean, errors = cprofile.normalize(body.get("profile"))
    if errors or not clean:
        raise HTTPException(400, detail="；".join(errors or ["档案不能为空"]))
    for name, value in clean.get('headers', {}).items():
        if any(c in name + value for c in '\r\n'):
            raise HTTPException(400, detail="请求头不能包含换行")
    # Validate the whole document before replacing anything; importing also
    # activates saved mode so an absent/stale installation cannot override it.
    result = cprofile.save(clean, new_source=cprofile.SOURCE_SAVED, db=db, merge=False)
    if not result.get("ok"):
        raise HTTPException(400, detail="；".join(result.get("errors") or ["导入失败"]))
    return {"ok": True, **_payload(db)}
