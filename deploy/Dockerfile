FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS admin
WORKDIR /web
COPY admin-web/package.json admin-web/package-lock.json ./
RUN npm ci
COPY admin-web ./
RUN npm run build

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
WORKDIR /app
COPY pyproject.toml README.md LICENSE requirements.lock ./
COPY server ./server
COPY providers ./providers
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home homeai \
    && mkdir -p /app/state && chown 10001:10001 /app/state && chmod 700 /app/state
COPY alembic.ini ./
COPY --from=admin /web/dist ./admin-web/dist
ENV HOMEAI_ADMIN_DIST=/app/admin-web/dist \
    HOMEAI_ENVIRONMENT=production \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8000
CMD ["uvicorn", "homeai.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
