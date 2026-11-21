FROM node:24-alpine@sha256:8a32c8cf649a49915a66e81e7a609223c708d0adabf288ee3f4077d765542f85 AS frontend
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS powerpoint
WORKDIR /pptx_runtime
COPY pptx_runtime/package*.json ./
RUN npm ci --ignore-scripts --omit=dev --no-audit --no-fund
COPY pptx_runtime/render.cjs ./

FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data \
    GIGACHAT_CA_BUNDLE=/app/certs/russian_trusted_root_ca_pem.crt \
    FINMODEL_ENV_FILE=/run/secrets/gigachat_env \
    FINMODEL_PPTX_RUNTIME=/app/pptx_runtime FINMODEL_NODE_PATH=/usr/local/bin/node
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src/ src/
RUN pip install --no-cache-dir --no-deps . && \
    useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
COPY certs/ certs/
COPY --from=frontend /ui/dist/ frontend/dist/
COPY --from=powerpoint /usr/local/bin/node /usr/local/bin/node
COPY --from=powerpoint /pptx_runtime/ pptx_runtime/
RUN node --version && node -e "require('/app/pptx_runtime/node_modules/pptxgenjs')"
ARG BUILD_SOURCE_SHA256=unknown
LABEL org.opencontainers.image.title="Financial model audit" \
      org.opencontainers.image.revision=$BUILD_SOURCE_SHA256
USER app
EXPOSE 8000
CMD ["uvicorn", "finmodel_audit.server:app", "--host", "0.0.0.0", "--port", "8000"]
