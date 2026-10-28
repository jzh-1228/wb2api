# Planned upgrades without nginx 502 responses

The independent waiting proxy runs on loopback port 8791. Normal traffic still
goes directly from nginx to port 8790; this proxy adds no normal-request hop.
It does not run application schedulers or access account credentials.

Install `wb2api-upgrade-gate.service` with paths adjusted for the host. Add
`upgrade-nginx.conf` inside the site's server block and this directive inside
the existing `location /` block:

```nginx
access_by_lua_file /www/wwwroot/workbuddy2api/deploy/upgrade-access.lua;
```

Validate nginx configuration before a graceful reload. Start and enable the
waiting service before enabling the nginx integration. Keep the waiting service
running independently during application deployments; never stop both together.

## Required deployment order

1. Stage and validate the new application and save a rollback manifest.
2. Run `python scripts/upgrade_control.py begin --seconds 180` before stopping
   the old process. Confirm `status` reports `upgrading: true`.
3. Run `python scripts/upgrade_control.py drain --seconds 90` using the same
   configuration/environment as the application. It checks the authenticated
   backend activity endpoint and reserves 30 seconds for the restart. The old
   supervisor has a 12-second termination allowance, so do not treat SIGTERM
   alone as sufficient to drain long model generations. Abort the deployment
   if active generations do not finish within the deployment budget.
4. Stop the supervisor, acquire the watchdog lock, replace the staged files,
   and restart with the saved environment. Never restart the waiting service
   or nginx as part of this step.
5. Verify the backend's version and readiness on loopback. Run
   `python scripts/upgrade_control.py end` to release queued requests.
6. Check public endpoints and ensure the waiting proxy's pending count returns
   to zero. On failure restore the previous code, verify it is healthy, then
   end the upgrade window. Do not restore an old database over newer writes.

The flag expires automatically after at most 180 seconds; requests already in
the proxy have their own 180-second deadline. Limit the full drain/restart
window to less than 180 seconds. A stale flag cannot leave the site permanently
in maintenance mode. Do not begin a second upgrade until pending requests from
the previous window finish.

## Client behavior

Chat Completions, Responses, and Messages streaming requests receive HTTP 200
SSE comments every three seconds during the wait. Comments carry the upgrade
notice; they are not assistant tokens, completion events, or billable usage.
When the backend is ready, the original method, URL, headers, and body are
forwarded once, and the backend's event stream continues on the same connection.
SSE comments may not be visible in a client's chat interface.

Non-streaming requests, including image generation and multipart image edits,
wait for the real backend response and retain its status and headers. Their
client-side request timeout must exceed the upgrade window.

Authentication and quota checks are performed by the real backend after waiting.
Once an SSE response has started, backend rejections use the corresponding
protocol's error event. Non-streaming errors retain their HTTP status. An
expired wait or a full queue returns a retryable error; no completed model
answer is fabricated. POSTs are never retried automatically after forwarding.

The proxy allows 128 simultaneous requests and 64 MiB per request, spooling
uploads larger than 1 MiB to disk. Cancellation releases the queued body and
slot. This mechanism does not recover a connection broken before the upgrade
window was enabled, nor a model stream forcibly terminated during generation.

## Docker builds in mainland China

`Dockerfile` accepts two build args so builds work without direct PyPI /
Docker Hub access:

* `BASE_IMAGE` — pull the base image through a registry mirror first, tag it
  back to `python:3.12-slim`, and build unchanged. Alternatively set
  `BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim`.
* `PIP_INDEX_URL` — pip index, defaults to `https://pypi.org/simple`. Use
  `https://pypi.tuna.tsinghua.edu.cn/simple` (or Aliyun
  `https://mirrors.aliyun.com/pypi/simple/`) inside China. `PIP_EXTRA_ARGS`
  passes anything extra (for example `--trusted-host`).

```bash
docker pull docker.m.daocloud.io/library/python:3.12-slim
docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
docker compose --env-file .env.docker build \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple admin
docker compose --env-file .env.docker up -d admin
```

The admin console's "更新与备份" page can render these mirror commands for
the latest tag: enable the 国内网络环境 toggle under step 02. Source-tree
updates still need `git fetch` to reach the origin; use a proxy or a mirror
for that hop as appropriate.

## Online backup restore (sqlite)

`POST /api/maintenance/restore` accepts a previously downloaded SQLite backup
as a raw request body. It validates integrity and required tables, saves the
current database as `<db>.pre-restore-<timestamp>` in the same directory, then
restores via the sqlite backup API. On any failure the safety copy is copied
back. Restart the service after restoring so in-memory pools match the disk
state. The console exposes this as the 恢复备份 button in 更新与备份.
