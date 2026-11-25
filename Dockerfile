FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROCUREAI_DB_PATH=/data/procureai.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code only; never bake the developer's local SQLite database into the image.
COPY main.py app.py procureai_store.py supplier_portal.py ./
COPY agentic ./agentic
COPY static ./static

RUN mkdir -p /data
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
