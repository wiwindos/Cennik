FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
WORKDIR /app
COPY requirements.server.lock.txt pyproject.toml ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.server.lock.txt \
    && useradd --uid 10001 --create-home cennik && mkdir /data && chown cennik:cennik /data
COPY src ./src
COPY release-seed ./release-seed
RUN pip install --no-cache-dir --no-deps .
USER 10001:10001
EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/healthz',timeout=3)"
CMD ["python", "-m", "cennik.server", "--root", "/data", "--host", "0.0.0.0", "--port", "8770"]
