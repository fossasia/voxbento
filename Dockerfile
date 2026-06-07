FROM python:3.14-slim

WORKDIR /app

# Install uv and ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

# Copy dependency manifests first for cache efficiency.
# Use --no-install-project so uv only installs third-party packages at this
# layer (the project itself is installed after the source is copied, which
# maximises Docker layer caching when only code — not deps — changes).
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

# Copy application code and install the project into the same venv
COPY . .
RUN uv sync --no-dev --frozen

EXPOSE 8000

# Run Alembic migrations then start uvicorn.
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn fastapi_app:app --host 0.0.0.0 --port 8000"]
