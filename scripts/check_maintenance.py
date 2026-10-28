"""Offline regression for issues 27/28 and adapted PR 26/29 fixes.

Uses its own temporary SQLite database; never reads deployment credentials.
Run: python scripts/check_maintenance.py
"""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from datetime import datetime
import json
import asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORK = tempfile.TemporaryDirectory(prefix="wb-maintenance-check-")
for name in list(os.environ):
    if name.startswith(("ADMIN_", "WORKBUDDY_", "CONVERTER_", "WB_DESKTOP_")):
        os.environ.pop(name)
os.environ.update(WORKBUDDY_DESKTOP="1", ADMIN_DATABASE_URL="sqlite:///" + (Path(WORK.name) / "check.db").as_posix(),
                  ADMIN_REDIS_URL="", ADMIN_GATEWAY_LIMIT_BACKEND="memory", ADMIN_DEV_TOOLS="0",
                  ADMIN_USERNAME="maintenance-check", ADMIN_PASSWORD="isolated-local-check",
                  ADMIN_BACKEND="http://127.0.0.1:1", CONVERTER_API_KEY="",
                  ADMIN_JWT_SECRET="isolated-test-signing-secret-with-more-than-32-bytes")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from admin.db import Base, engine, SessionLocal, init_db
from admin.models import Account, CreditEvent, GrowthPlanRecord, SystemSetting
from admin import credit_journal
from admin.growth_center import record_result
from admin.routers import maintenance, accounts
from admin.security import create_admin_token
from admin.pool import PoolRuntime
from responses_projection import project_responses_chat_body
from desensitize import desensitize_body

app = FastAPI()
app.include_router(maintenance.router)
app.include_router(accounts.router)


class Checks(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        self.assertTrue(init_db())
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"X-Admin-Token": create_admin_token()}
        self.account = Account(name="Example", uid="synthetic", auth_json="{}", balance_remain=100, last_sync_at=datetime.utcnow())
        self.db.add(self.account)
        self.db.commit()

    def test_caps_maintenance_and_uid_leases(self):
        pool = PoolRuntime(); pool.max_in_flight = 3
        one = SimpleNamespace(uid="one", max_in_flight=1)
        unlimited = SimpleNamespace(uid="unlimited", max_in_flight=0)
        inherited = SimpleNamespace(uid="inherited", max_in_flight=None)
        self.assertTrue(pool.acquire_account(one))
        self.assertFalse(pool.acquire_account(one))
        self.assertNotIn(one, pool.available([one]))
        self.assertEqual(pool.limit_for(inherited),3)
        self.assertTrue(pool.inflight.acquire_maintenance(unlimited.uid))
        self.assertNotIn(unlimited, pool.available([unlimited]))
        self.assertFalse(pool.acquire_account(unlimited))
        pool.inflight.release_maintenance(unlimited.uid)
        for _ in range(20): self.assertTrue(pool.acquire_account(unlimited))
        pool.release(one.uid)
        self.assertTrue(pool.acquire_account(one))

    def test_strict_cap_validation_and_auth(self):
        path = "/api/accounts/" + str(self.account.id)
        self.assertEqual(self.client.patch(path,json={"max_in_flight":0}).status_code,401)
        for value in (True, 1.5, -1, "3", 2147483648, {}, []):
            self.assertEqual(self.client.patch(path,json={"max_in_flight":value},headers=self.headers).status_code,400,value)
        for value in (None, 0, 1, 8):
            self.assertEqual(self.client.patch(path,json={"max_in_flight":value},headers=self.headers).status_code,200)
            self.db.refresh(self.account)
            self.assertEqual(self.account.max_in_flight,value)

    def test_account_specific_cap_waits_for_release(self):
        from admin.routers import proxy
        pool=PoolRuntime();pool.max_in_flight=3
        self.account.max_in_flight=1;self.db.commit()
        self.assertTrue(pool.acquire_account(self.account))
        def select(*args,**kwargs):
            return self.account if pool.inflight.count(self.account.uid)==0 else None
        async def run():
            async def release():
                await asyncio.sleep(.05);pool.release(self.account.uid)
            task=asyncio.create_task(release())
            with patch.object(proxy,"POOL",pool),patch.object(proxy,"_select_account",side_effect=select):
                result=await proxy._select_account_with_wait(self.db,model="synthetic")
            await task
            return result
        self.assertIs(asyncio.run(run()),self.account)

    def test_reward_split_dedup_and_observation(self):
        record_result(self.db,self.account.id,{"ok":False,"credit":12,"steps":[
            {"step":"签到","ok":True,"reward":5},{"step":"兑换","ok":True,"reward":7},
            {"step":"失败","ok":False,"reward":99}]},source="schedule")
        events = self.db.query(CreditEvent).all()
        self.assertEqual(sum(e.delta for e in events),12)
        self.assertEqual(len(events),2)
        self.assertTrue(all(e.balance_after is None for e in events))
        credit_journal.backfill(self.db); credit_journal.backfill(self.db)
        self.assertEqual(self.db.query(CreditEvent).count(),2)
        self.account.balance_remain=112; self.db.commit()
        observed=self.db.query(CreditEvent).filter_by(kind="observed").one()
        self.assertEqual((observed.delta,observed.balance_after),(12,112))
        self.db.rollback()
        self.account.balance_remain=999; self.db.flush(); self.db.rollback()
        self.assertEqual(self.db.query(CreditEvent).filter_by(kind="observed").count(),1)
        result={"ok":True,"credit":500,"credit_event_key":"benefit:synthetic"}
        record_result(self.db,self.account.id,result,action="补偿")
        record_result(self.db,self.account.id,result,action="补偿")
        self.assertEqual(self.db.query(CreditEvent).filter_by(delta=500).count(),1)
        record=self.db.query(GrowthPlanRecord).order_by(GrowthPlanRecord.id.desc()).first()
        credit_journal.attach_balance(self.db,[record.id],612);self.db.commit()
        self.assertEqual(self.db.query(CreditEvent).filter_by(delta=500).one().balance_after,612)

    def test_new_account_baseline_and_history(self):
        baseline=Account(name="New",uid="new",auth_json="{}",balance_remain=0)
        self.db.add(baseline);self.db.commit()
        baseline.balance_remain=500;baseline.last_sync_at=datetime.utcnow();self.db.commit()
        self.assertEqual(self.db.query(CreditEvent).count(),0)
        record=GrowthPlanRecord(account_id=self.account.id,credit=5,source="schedule",action="旧签到",ok=1,result_json="{}")
        self.db.add(record);self.db.commit()
        credit_journal.backfill(self.db);credit_journal.backfill(self.db)
        self.assertEqual(self.db.query(CreditEvent).count(),1)
        self.assertIsNone(self.db.query(CreditEvent).one().balance_after)
        self.db.delete(self.account);self.db.commit()
        data=self.client.get("/api/maintenance/credit-events",headers=self.headers).json()
        self.assertEqual(data["total"],1)
        self.assertEqual(data["accounts"][0]["name"],"Example")

    def test_filters_pagination_and_auth(self):
        for i in range(3): record_result(self.db,self.account.id,{"ok":True,"credit":i+1},action="签到")
        path="/api/maintenance/credit-events"
        self.assertEqual(self.client.get(path).status_code,401)
        data=self.client.get(path,headers=self.headers,params={"page_size":2}).json()
        self.assertEqual((data["total"],len(data["items"])),(3,2))
        self.assertEqual(self.client.get(path,headers=self.headers,params={"account_id":999}).json()["total"],0)
        self.assertEqual(self.client.get(path,headers=self.headers,params={"source":"schedule · 签到","kind":"confirmed"}).json()["total"],3)
        self.assertEqual(self.client.get(path,headers=self.headers,params={"start":"2099-01-01T00:00:00Z"}).json()["total"],0)
        for params in ({"kind":"invalid"},{"start":"2026-01-02","end":"2026-01-01"},{"page":0}):
            self.assertGreaterEqual(self.client.get(path,headers=self.headers,params=params).status_code,400)

    def test_backup_integrity_permissions_cleanup_and_mysql(self):
        path="/api/maintenance/backup"
        self.assertEqual(self.client.post(path).status_code,401)
        paths=[]
        original=tempfile.mkstemp
        def create(*args,**kwargs):
            fd,name=original(*args,**kwargs);paths.append(Path(name));return fd,name
        with patch.object(maintenance.tempfile,"mkstemp",side_effect=create):
            response=self.client.post(path,headers=self.headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers["cache-control"],"no-store")
        self.assertTrue(all(not path.exists() for path in paths))
        target=Path(WORK.name)/"restored.db";target.write_bytes(response.content)
        from contextlib import closing
        with closing(sqlite3.connect(target)) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(backup.execute("SELECT name FROM accounts").fetchone()[0],"Example")
        fake=SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
        with patch.object(maintenance,"engine",fake):
            self.assertEqual(self.client.post(path,headers=self.headers).status_code,409)

    def test_update_release_without_client_assets(self):
        self.assertEqual(self.client.get("/api/maintenance/update").status_code,401)
        fake=Mock();fake.check.return_value={"releases":[{"tag":"v9.0.0","name":"next","url":"https://github.com/xiaofan6ya/wb2api/releases/tag/v9.0.0"},{"tag":"invalid"},{"tag":"v99.0.0rc1"}],"error":""}
        with patch.object(maintenance,"service",return_value=fake):
            data=self.client.get("/api/maintenance/update",headers=self.headers).json()
        self.assertEqual(data["latest"]["tag"],"v9.0.0")
        self.assertTrue(data["available"])
        self.assertIn("--env-file .env.docker",data["commands"]["docker"])

    def test_static_mime_and_remote_admin_auth(self):
        import mimetypes
        mimetypes.add_type("text/plain",".js")
        from admin.server import app as server
        client=TestClient(server)
        self.addCleanup(client.close)
        self.assertIn("javascript",client.get("/static/review-improvements.js").headers["content-type"])
        self.assertEqual(client.get("/api/accounts").status_code,401)
        self.assertEqual(client.get("/api/accounts",headers=self.headers).status_code,200)

    def test_mixed_user_images_tool_descriptions_roundtrip(self):
        content=[{"type":"text","text":"<system-reminder>runtime only</system-reminder>\n/goal clear"},{"type":"image_url","image_url":{"url":"https://example.org/image.png"}}]
        body={"messages":[{"role":"user","content":content}],"tools":[{"type":"function","function":{"name":"clear_goal","description":"Clear the active goal on explicit user request.","parameters":{"type":"object","properties":{"reason":{"type":"string","description":"Explain why to clear."}}}}}]}
        projected,_=project_responses_chat_body(body)
        projected=desensitize_body(projected,desensitize_tools=True,strip_tool_metadata=False)
        self.assertEqual(projected["messages"][-1]["content"][0]["text"],"/goal clear")
        self.assertEqual(projected["messages"][-1]["content"][1],content[1])
        function=projected["tools"][0]["function"]
        self.assertIn("explicit",function["description"])
        self.assertIn("description",function["parameters"]["properties"]["reason"])
        self.assertIn("runtime only",body["messages"][0]["content"][0]["text"])
        projected,_=project_responses_chat_body({"messages":[{"role":"user","content":"<system-reminder>runtime only</system-reminder>"}]})
        self.assertNotIn("runtime only",json.dumps(projected))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        engine.dispose()
        WORK.cleanup()
