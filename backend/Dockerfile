
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libglib2.0-0 \
    libgl1 \
    git-lfs \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir gunicorn

COPY backend/ /app/

# Copy manuals from the repository root.
COPY manuals/ /app/manuals/

# Verify that PDFs are actual files, not Git LFS pointers.
RUN test -n "$(find /app/manuals -maxdepth 1 -type f -name '*.pdf' -print -quit)" \
    && ! grep -a -l -m 1 'version https://git-lfs.github.com/spec/v1' /app/manuals/*.pdf

CMD sh -c 'gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-10000}'
