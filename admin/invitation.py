"""WorkBuddy referral configuration and official binding contract."""
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from admin.db import SessionLocal
from admin.models import SystemSetting

SETTING_KEY = "workbuddy_invite_code"
HOST = "https://www.workbuddy.cn"
BIND_PATH = "/activity/workbuddy/invitation/v2/bind"


def normalize(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" in value:
        try:
            url = urlsplit(value)
            codes = parse_qs(url.query).get("inviteCode", [])
            valid = (url.scheme == "https" and url.hostname == "www.workbuddy.cn"
                     and url.port in (None, 443) and not url.username and not url.password
                     and url.path.rstrip("/") == "/events/invite" and len(codes) == 1)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("请填写 WorkBuddy 官方邀请链接或邀请码")
        value = codes[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", value):
        raise ValueError("邀请码须为 6 至 128 位字母、数字、下划线或连字符")
    return value


def invite_url(code: str) -> str:
    return HOST + "/events/invite?" + urlencode({"inviteCode": code}) if code else ""


def current(db=None) -> dict:
    own = db is None
    db = SessionLocal() if own else db
    try:
        row = db.get(SystemSetting, SETTING_KEY)
        code = normalize(row.value) if row and row.value else ""
        return {"invite_code": code, "invite_url": invite_url(code), "enabled": bool(code)}
    finally:
        if own:
            db.close()


def save(value: str, db) -> dict:
    code = normalize(value)
    row = db.get(SystemSetting, SETTING_KEY)
    if row is None:
        row = SystemSetting(key=SETTING_KEY)
        db.add(row)
    row.value = code
    db.commit()
    return current(db)
