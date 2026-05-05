FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency install step isolated so layer cache survives code edits.
COPY pyproject.toml /app/
RUN pip install --upgrade pip && \
    mkdir -p /app/src/temporal_console && \
    touch /app/src/temporal_console/__init__.py && \
    pip install -e '.'

# Copy the real package on top of the stub.
COPY src/ /app/src/

RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "temporal_console.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
