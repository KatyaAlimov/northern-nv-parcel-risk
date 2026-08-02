FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config/ config/
COPY config_loader.py regions_loader.py spatial_ops.py parcel_lookup.py risk_engine.py 05_serve_city_map.py app.py ./
COPY templates/ templates/

ENV PYTHONUNBUFFERED=1
EXPOSE 8081 8501

# Default: city-map lookup API. Compose overrides for Streamlit.
CMD ["python3", "05_serve_city_map.py", "--host", "0.0.0.0", "--port", "8081", "--dir", "outputs"]
