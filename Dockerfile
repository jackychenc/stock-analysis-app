# Shared image: FastAPI api + batch entrypoints (compose overrides command).
FROM python:3.12-slim

WORKDIR /srv/app

COPY pyproject.toml ./
RUN pip install --no-cache-dir uv && uv pip install --system -r pyproject.toml

COPY app ./app
COPY db ./db
COPY scripts ./scripts
COPY config ./config
COPY openapi.yaml ./

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
