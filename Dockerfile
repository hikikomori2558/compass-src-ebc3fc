# COMPASS-HYBRID v2.6 — двухстадийная сборка (доки Docker: multi-stage builds).
# Builder: gcc живёт только здесь — он компилирует C-ядро и ставит зависимости.
# Runtime: slim без компилятора и dev-заголовков — меньше размер и поверхность атаки.
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Копируем код и собираем libcompass_core.so внутри образа
COPY c_core/ /app/c_core/
COPY clinical_data/ /app/clinical_data/
COPY backend/ /app/backend/
COPY benchmarks/ /app/benchmarks/
COPY scripts/ /app/scripts/
RUN python3 scripts/build_c_core.py

# ----------------------------------------------------------------------------
FROM python:3.11-slim

# В рантайме не нужен ни gcc, ни заголовки — только venv с зависимостями,
# код и уже собранная нативная библиотека.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
