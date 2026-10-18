FROM python:3.12-slim
WORKDIR /app
# F8 (ГОСТ 56939): закреплённые версии deps — воспроизводимая сборка.
# requirements-lock.txt = pip freeze снимок; локальные пакеты ставятся --no-deps
# (транзитивные зависимости уже зафиксированы lockfile'ом).
COPY api/requirements-lock.txt /app/api/requirements-lock.txt
RUN pip install --no-cache-dir -r /app/api/requirements-lock.txt
COPY core/ /app/core/
COPY api/ /app/api/
RUN pip install --no-cache-dir --no-deps /app/core /app/api
WORKDIR /app/api
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]