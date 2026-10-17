# COMPASS-HYBRID v2.6 — образ главного API (PostgreSQL/Redis поднимает docker-compose.yml)
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY c_core/ /app/c_core/
COPY clinical_data/ /app/clinical_data/
COPY backend/ /app/backend/
COPY benchmarks/ /app/benchmarks/
COPY scripts/ /app/scripts/

# Собираем libcompass_core.so внутри образа (в git и в образ не попадает мусора *.o)
RUN python3 scripts/build_c_core.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
