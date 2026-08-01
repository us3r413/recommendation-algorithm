FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ src/
COPY app.py .
COPY .env .

# Copy dataset files (all CSVs and pkl if present)
# Note: ensure dataset/ contains the generated CSVs before building
COPY dataset/ dataset/

EXPOSE 8000

# Run with uvicorn — single worker is fine for demo/competition
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
