# 国内网络构建示例（基础镜像与 pip 都可换源）：
#   docker pull docker.m.daocloud.io/library/python:3.12-slim
#   docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
#   docker compose --env-file .env.docker build \
#     --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple admin
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_ARGS=""

COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} ${PIP_EXTRA_ARGS} -r requirements.txt

COPY converter.py responses_adapter.py responses_projection.py anthropic_adapter.py desensitize.py upstream_compat.py repository_instructions.py model_metadata.py wb_install.py wb_asar.py wb_envcheck.py turing_helper.js ./
COPY admin ./admin
COPY desktop/__init__.py desktop/common.py desktop/runtime.py desktop/updates.py desktop/version.json ./desktop/
COPY desktop/assets ./desktop/assets

EXPOSE 8790 8787

CMD ["python3", "-m", "uvicorn", "admin.server:app", "--host", "0.0.0.0", "--port", "8790"]
