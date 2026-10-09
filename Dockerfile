FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AHL_DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml config.toml ./
COPY src ./src
RUN pip install --no-cache-dir '.[web]' && useradd --uid 10001 --create-home ahledger && mkdir /data && chown ahledger:ahledger /data
USER ahledger
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "ahledger.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
