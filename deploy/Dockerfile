FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY server ./server
COPY providers ./providers
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home homeai
COPY alembic.ini ./
USER 10001
EXPOSE 8000
CMD ["uvicorn", "homeai.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
