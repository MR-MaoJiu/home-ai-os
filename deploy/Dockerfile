FROM node:22-alpine AS admin
WORKDIR /web
COPY admin-web/package.json admin-web/package-lock.json ./
RUN npm ci
COPY admin-web ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY server ./server
COPY providers ./providers
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home homeai
COPY alembic.ini ./
COPY --from=admin /web/dist ./admin-web/dist
USER 10001
EXPOSE 8000
CMD ["uvicorn", "homeai.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
