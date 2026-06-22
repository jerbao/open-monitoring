FROM python:3.14-slim

RUN groupadd -r app && useradd -r -g app app
WORKDIR /app

# context is the repo root (k8s/GHCR workflow uses context: .);
# everything here lives in backend/, so we prefix with backend/.
COPY backend/pyproject.toml ./pyproject.toml
RUN pip install --no-cache-dir .

COPY backend/app.py ./app.py
COPY backend/probes.yaml ./probes.yaml

# Empty static dir by default — in the cluster, k8s mounts the html/ ConfigMap
# here; in the benchmark compose it's just to satisfy StaticFiles().
RUN mkdir -p /app/static && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--loop", "uvloop", "--http", "httptools", "--backlog", "4096"]
