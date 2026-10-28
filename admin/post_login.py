"""Persistent onboarding with separate bounded reward and resource workers."""
import json
import logging
import queue
import threading
from datetime import datetime

from admin.db import SessionLocal
from admin.models import Account, SystemSetting
from admin.upstream_errors import UpstreamError

_PREFIX = 'post_login_automation:'
_queue = queue.Queue()
_workers = []
_lock = threading.Lock()
_pending = set()
_log = logging.getLogger(__name__)
_RESOURCE_PREFIX = 'account_resources:'
_resources = queue.Queue()
_resource_workers = []
_resource_pending = set()
_model_init_lock = threading.Lock()


def _resource_region_issue(auth_json):
    try:
        auth = json.loads(auth_json).get('auth') or {}
        domain = str(auth.get('domain') or '').lower().strip().rstrip('.')
    except (ValueError, TypeError, AttributeError):
        return ''
    if domain in ('workbuddy.ai', 'www.workbuddy.ai', 'codebuddy.ai', 'www.codebuddy.ai'):
        return '国际版账号凭证无法访问当前国内接口，请导入 WorkBuddy 国内版凭证'
    return ''


def _resource_worker():
    while True:
        account_id = _resources.get()
        try:
            _save_resource(account_id, 'running')
            from admin import backend
            from admin.models import ModelConfig
            from admin.routers.models import _do_sync_models
            errors = []
            with SessionLocal() as db:
                acc = db.query(Account).filter(Account.id == account_id).first()
                if not acc:
                    continue
                original = acc.auth_json
            region_issue = _resource_region_issue(original)
            if region_issue:
                _save_resource(account_id, 'unsupported_region', [region_issue])
                _log.warning('跳过资源初始化 account_id=%s：%s', account_id, region_issue)
                continue
            try:
                with backend.AccountSession(original) as session:
                    balance = session.fetch_balance()
                    from admin.routers.accounts import _sync_credit_snapshot
                    try:
                        _sync_credit_snapshot(acc, session.fetch_credit_details())
                    except Exception:
                        errors.append('积分到期快照刷新失败，可在账号页重试')
                    updated = session.updated_json()
                with SessionLocal() as db:
                    # Never overwrite a more recent login or token refresh.
                    updates = {
                        Account.auth_json: updated,
                        Account.balance_total: int(balance.get('total') or 0),
                        Account.balance_remain: int(balance.get('remain') or 0),
                        Account.last_sync_at: datetime.utcnow()}
                    if acc.credits_synced_at is not None:
                        for field in ('credits_snapshot', 'credits_synced_at', 'credits_expiring',
                                      'credits_expiring_soon', 'credits_evergreen', 'credits_expired',
                                      'credits_soonest_expire_at'):
                            updates[getattr(Account, field)] = getattr(acc, field)
                    changed = db.query(Account).filter(Account.id == account_id, Account.auth_json == original).update(
                        updates, synchronize_session=False)
                    db.commit()
                    if not changed:
                        errors.append('账号凭证已变更，请重新刷新资源')
            except backend.CredentialError as exc:
                _save_resource(account_id, 'invalid_credential', [str(exc)])
                _log.warning('账号凭证不完整，跳过资源初始化 account_id=%s', account_id)
                continue
            except UpstreamError as exc:
                if exc.http_status in (401, 403):
                    _save_resource(account_id, 'auth_required', ['上游拒绝鉴权，请重新授权登录后刷新余额'])
                    _log.warning('资源初始化鉴权失败 account_id=%s HTTP=%s，请重新授权登录', account_id, exc.http_status)
                    continue
                errors.append('余额刷新失败，可在账号页重试')
                _log.warning('新增账号余额刷新失败 account_id=%s HTTP=%s code=%s',
                             account_id, exc.http_status, exc.upstream.get('code'))
            except Exception:
                errors.append('余额刷新失败，可在账号页重试')
                _log.exception('新增账号余额刷新失败 account_id=%s', account_id)
            try:
                # First-account initialization is independent of the slow reward
                # queue. Preserve existing model toggles on subsequent imports.
                with _model_init_lock, SessionLocal() as db:
                    if db.query(ModelConfig).first() is None:
                        _do_sync_models(db, account_id=account_id)
            except Exception:
                errors.append('初始模型同步失败，可在模型页重试')
                _log.exception('初始模型同步失败 account_id=%s', account_id)
            _save_resource(account_id, 'failed' if errors else 'completed', errors)
        except Exception:
            _log.exception('新增账号资源初始化失败 account_id=%s', account_id)
            try:
                _save_resource(account_id, 'failed', ['资源初始化失败，重启后会自动重试'])
            except Exception:
                _log.exception('资源初始化状态保存失败 account_id=%s', account_id)
        finally:
            with _lock:
                _resource_pending.discard(account_id)
            _resources.task_done()


def _save_resource(account_id, status, errors=None):
    with SessionLocal() as db:
        key = _RESOURCE_PREFIX + str(account_id)
        row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row is None:
            row = SystemSetting(key=key)
            db.add(row)
        row.value = json.dumps({'status': status, 'errors': errors or [],
                                'updated_at': datetime.utcnow().isoformat()}, ensure_ascii=False)
        db.commit()


def enqueue_resources(account_id):
    """Balance/model initialization must not wait for growth tasks to finish."""
    try:
        with _lock:
            if account_id in _resource_pending:
                return
            _save_resource(account_id, 'queued')
            _resource_pending.add(account_id)
            while len(_resource_workers) < 2:
                worker = threading.Thread(target=_resource_worker, daemon=True, name='wb-account-resources')
                worker.start()
                _resource_workers.append(worker)
            _resources.put(account_id)
    except Exception:
        _log.exception('新增账号资源任务提交失败 account_id=%s', account_id)


def _dispatch():
    while True:
        fn, args = _queue.get()
        try:
            fn(*args)
        except Exception:
            _log.exception('自动任务队列执行失败')
        finally:
            _queue.task_done()


def _submit(fn, *args):
    # Called under _lock. Daemon workers do not delay desktop shutdown;
    # persistent queued/running states are recovered at the next startup.
    while len(_workers) < 2:
        worker = threading.Thread(target=_dispatch, daemon=True,
                                  name='wb-post-login-' + str(len(_workers)))
        worker.start()
        _workers.append(worker)
    _queue.put((fn, args))


def _save(account_id, status, result=None):
    with SessionLocal() as db:
        key = _PREFIX + str(account_id)
        row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row is None:
            row = SystemSetting(key=key)
            db.add(row)
        row.value = json.dumps({'status': status, 'updated_at': datetime.utcnow().isoformat(),
                                'result': result}, ensure_ascii=False)
        db.commit()


def _worker(account_id, name):
    try:
        _save(account_id, 'running')
        from admin.routers.login import _run_post_login_automation
        result = _run_post_login_automation(account_id, name)
        _save(account_id, 'failed' if result and result.get('errors') else 'completed', result)
    except Exception:
        _log.exception('登录后自动任务失败 account_id=%s', account_id)
        try:
            _save(account_id, 'failed', {'errors': ['自动任务执行异常，可重新执行成长任务']})
        except Exception:
            _log.exception('自动任务状态保存失败 account_id=%s', account_id)
    finally:
        with _lock:
            _pending.discard(account_id)


def enqueue(account_id, name):
    with _lock:
        if account_id in _pending:
            return
        # Persist before submitting: a restart must not silently lose queued accounts.
        _save(account_id, 'queued')
        _pending.add(account_id)
        try:
            _submit(_worker, account_id, name)
        except Exception:
            _pending.discard(account_id)
            raise


def resume():
    """Called once after database initialization; rewards are claimed idempotently."""
    with SessionLocal() as db:
        rows = db.query(SystemSetting).filter(SystemSetting.key.like(_PREFIX + '%')).all()
        todo = []
        for row in rows:
            try:
                state = json.loads(row.value)
                account_id = int(row.key[len(_PREFIX):])
            except (ValueError, TypeError):
                continue
            if not isinstance(state, dict):
                continue
            if state.get('status') not in ('queued', 'running'):
                continue
            acc = db.query(Account).filter(Account.id == account_id, Account.status == 'active').first()
            if acc:
                todo.append((acc.id, acc.name))
    for account_id, name in todo:
        enqueue(account_id, name)
    from admin.models import ModelConfig
    with SessionLocal() as db:
        empty_models = db.query(ModelConfig).first() is None
        resource_todo = set()
        blocked_resources = set()
        for row in db.query(SystemSetting).filter(SystemSetting.key.like(_RESOURCE_PREFIX + '%')):
            try:
                status = json.loads(row.value).get('status')
                if status in ('unsupported_region', 'auth_required', 'invalid_credential'):
                    blocked_resources.add(int(row.key[len(_RESOURCE_PREFIX):]))
                if status in ('queued', 'running', 'failed'):
                    resource_todo.add(int(row.key[len(_RESOURCE_PREFIX):]))
            except (ValueError, TypeError, AttributeError):
                pass
        active = [a for a in db.query(Account).filter(Account.status == 'active').all()
                  if a.id not in blocked_resources]
        resource_todo.update(a.id for a in active if a.last_sync_at is None)
        if empty_models and active:
            resource_todo.add(active[0].id)
        resource_todo.intersection_update(a.id for a in active)
    for account_id in resource_todo:
        enqueue_resources(account_id)


def states(db, account_ids, prefix=_PREFIX):
    keys = [prefix + str(aid) for aid in account_ids]
    if not keys:
        return {}
    out = {}
    for row in db.query(SystemSetting).filter(SystemSetting.key.in_(keys)).all():
        try:
            value = json.loads(row.value)
            if isinstance(value, dict):
                out[int(row.key[len(prefix):])] = value
        except (ValueError, TypeError):
            pass
    return out
