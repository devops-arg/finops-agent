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

EXPOSE 8000

CMD ["python", "run_server.py"]
