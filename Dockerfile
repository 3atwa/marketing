FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LEADFORGE_DB=/app/data/leadforge.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY leadforge ./leadforge
RUN mkdir -p /app/data
EXPOSE 8000
CMD ["uvicorn", "leadforge.main:app", "--host", "0.0.0.0", "--port", "8000"]
