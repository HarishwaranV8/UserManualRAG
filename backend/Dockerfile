
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn

COPY backend/ /app/
COPY manuals/ /app/manuals/

CMD sh -c 'gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000}'
