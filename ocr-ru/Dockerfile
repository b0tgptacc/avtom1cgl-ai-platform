FROM python:3.12-slim

# шрифты нужны только тестам при сборке (кириллица и иероглифы)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt fetch_models.py ./
RUN pip install --no-cache-dir -r requirements.txt
ENV TESSDATA_PREFIX=/usr/share/tessdata/
RUN python fetch_models.py

COPY ocr.py app.py ./
COPY tests ./tests

# прогон тестов при сборке: образ с неработающим распознаванием не соберётся
RUN OCR_STORE=/tmp/ocr-test python -m pytest -q -p no:cacheprovider tests && rm -rf /tmp/ocr-test

RUN useradd --system --uid 10002 ocr && mkdir -p /tmp/ocr-files && chown ocr /tmp/ocr-files
USER ocr

EXPOSE 8000
# процесс ~0,3–0,5 ГБ ОЗУ; страница A4 — 1–2 с
ENV GUNICORN_WORKERS=2
CMD ["sh", "-c", "exec gunicorn -w \"$GUNICORN_WORKERS\" -b 0.0.0.0:8000 --timeout 600 app:app"]
