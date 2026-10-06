FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first for better layer caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code
COPY main.py .
COPY src/ src/
COPY sql/ sql/

# Run as a non-root user; data/ and logs/ are bind-mounted by docker-compose
RUN useradd --create-home --uid 1000 etl \
    && mkdir -p data/raw data/processed data/rejected logs \
    && chown -R etl:etl /app
USER etl

ENTRYPOINT ["python", "main.py"]
CMD []
