"""Begin/end a bounded upgrade window before stopping the application."""
import argparse
import os
from pathlib import Path
import sys
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['begin', 'drain', 'end', 'status'])
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--marker', default='/run/wb2api-upgrade/active')
    parser.add_argument('--gate', default='http://127.0.0.1:8791')
    parser.add_argument('--backend', default='http://127.0.0.1:8790')
    args = parser.parse_args()
    marker = Path(args.marker)
    with httpx.Client(trust_env=False, timeout=5) as client:
        health = client.get(args.gate + '/__upgrade_health')
        health.raise_for_status()
        if args.action == 'status':
            print(health.text)
        elif args.action == 'drain':
            if not health.json()['upgrading']:
                raise SystemExit('Enable the upgrade window before draining')
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from admin.security import create_admin_token
            deadline = min(time.monotonic() + args.seconds,
                           time.monotonic() + max(0, float(marker.read_text()) - time.time() - 30))
            while time.monotonic() < deadline:
                response = client.get(args.backend + '/api/logs/requests?limit=1',
                                      headers={'X-Admin-Token': create_admin_token()})
                response.raise_for_status()
                if response.json()['active_count'] == 0:
                    print('Active model requests drained')
                    break
                time.sleep(.5)
            else:
                raise SystemExit('Drain timed out; keep the old application running and end the window')
        elif args.action == 'begin':
            if not 1 <= args.seconds <= 180:
                parser.error('--seconds must be between 1 and 180')
            if health.json()['upgrading'] or health.json()['pending']:
                raise SystemExit('An upgrade window or queued requests are already active')
            marker.parent.mkdir(parents=True, exist_ok=True)
            temporary = marker.with_suffix('.new')
            temporary.write_text(str(time.time() + args.seconds), encoding='ascii')
            os.chmod(temporary, 0o644)
            temporary.replace(marker)
            print('Upgrade waiting enabled')
        else:
            client.get(args.backend + '/api/app/version').raise_for_status()
            marker.unlink(missing_ok=True)
            print('Backend ready; queued requests released')


if __name__ == '__main__':
    main()
