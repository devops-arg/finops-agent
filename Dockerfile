FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY scripts/ scripts/
COPY run_server.py .
# report_data.json is optional — created at runtime if missing
COPY report_data.json* ./

# Create data directory for SQLite findings database.
# This directory is mounted as a Docker named volume (finops-data)
# so the database survives container restarts and image rebuilds.
RUN mkdir -p /app/data && chmod 777 /app/data

EXPOSE 8000

CMD ["python", "run_server.py"]
