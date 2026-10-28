"""Check application startup and request validation in an isolated empty database."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--android', action='store_true')
    parser.add_argument('--source-dir', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_dir.resolve()))
    # Never read the developer's .env or access their account database.
    for name in list(os.environ):
        if name.startswith(('ADMIN_', 'CONVERTER_', 'WORKBUDDY_', 'WB_DESKTOP_')):
            os.environ.pop(name)
    with tempfile.TemporaryDirectory(prefix='wb-runtime-check-') as work:
        os.environ.update({
            'WORKBUDDY_ANDROID' if args.android else 'WORKBUDDY_DESKTOP': '1',
            'ADMIN_DATABASE_URL': 'sqlite:///' + (Path(work) / 'check.db').as_posix(),
            'ADMIN_GATEWAY_LIMIT_BACKEND': 'memory', 'ADMIN_REDIS_URL': '',
            'ADMIN_USERNAME': 'runtime-check', 'ADMIN_PASSWORD': secrets.token_urlsafe(32),
            'ADMIN_JWT_SECRET': secrets.token_urlsafe(48), 'ADMIN_DEV_TOOLS': '0',
        })
        import fastapi
        import pydantic
        from fastapi.testclient import TestClient
        from admin.server import app
        from admin.db import engine
        from admin.security import create_admin_token

        try:
            with TestClient(app) as client:
                assert client.get('/api/app/version').status_code == 200
                assert client.get('/admin').status_code == 200
                assert client.get('/static/join.html').status_code == 200
                assert client.post('/v1/audio/transcriptions').status_code == 401
                from zoneinfo import ZoneInfo
                assert str(ZoneInfo('Asia/Shanghai')) == 'Asia/Shanghai'
                headers = {'X-Admin-Token': create_admin_token()}
                import jwt
                from admin.config import settings
                shell_token = jwt.encode({'sub': settings.ADMIN_USERNAME, 'local_shell': True,
                                          'iat': 1}, settings.JWT_SECRET, algorithm='HS256')
                assert client.get('/api/accounts', headers={'X-Admin-Token': shell_token}).status_code == 200
                assert client.post('/api/accounts/health-check-batch', headers=headers,
                                   json={'account_ids': list(range(5001))}).status_code == 422
                assert client.post('/api/accounts/health-check-batch',
                                   json={}).status_code == 401
                # The authenticated route must be registered without starting
                # any upstream work when the isolated database has no accounts.
                assert client.post('/api/accounts/health-check-batch', headers=headers,
                                   json={}).status_code == 400
                assert client.post('/api/accounts/999999/health-check',
                                   headers=headers).status_code == 404
                assert client.get('/api/benefits/compensation', headers=headers).status_code == 404
                assert client.get('/static/index.html').status_code == 404
                assert client.get('/api/accounts', headers=headers).status_code == 200
            print(f'Runtime startup and batch validation passed: FastAPI {fastapi.__version__}, '
                  f'Pydantic {pydantic.__version__}, Android={args.android}')
        finally:
            engine.dispose()


if __name__ == '__main__':
    main()
