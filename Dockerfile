FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency files first (better layer caching)
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-cache

# Copy application code
COPY app ./app
COPY utils ./utils
COPY alembic ./alembic
COPY alembic.ini ./

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.core.main:app", "--host", "0.0.0.0", "--port", "8000"]