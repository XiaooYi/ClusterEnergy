ARG CE_PYTHON_BASE=python:3.11-slim-bookworm
ARG CE_NGINX_BASE=nginx:1.28-alpine
FROM ${CE_PYTHON_BASE} AS backend
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY common /app/common

FROM backend AS a
COPY a_node /app/a_node
COPY c_node/collector.py /app/c_node/collector.py
ENTRYPOINT ["python", "/app/a_node/aggregator.py"]

FROM backend AS c
COPY c_node /app/c_node
ENTRYPOINT ["python", "/app/c_node/exporter.py"]

FROM backend AS gateway
COPY b_node/gateway /app/b_node/gateway
ENTRYPOINT ["python", "/app/b_node/gateway/gateway.py"]

FROM ${CE_NGINX_BASE} AS web
COPY b_node/frontend/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY b_node/frontend/dist /usr/share/nginx/html
EXPOSE 18088
