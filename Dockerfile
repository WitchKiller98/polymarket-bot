FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root user for security
RUN useradd --create-home appuser
USER appuser

# Paper mode by default – override CMD for live
ENTRYPOINT ["python", "main.py"]
