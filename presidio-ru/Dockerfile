FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY recognizers.py app.py ./
COPY tests ./tests

# прогон тестов при сборке: образ с неработающим распознаванием не соберётся
RUN python -m pytest -q -p no:cacheprovider tests

RUN useradd --system --uid 10001 presidio
USER presidio

EXPOSE 3000
# процессов — GUNICORN_WORKERS (по ~0,5 ГБ ОЗУ); большие тексты обработчик LiteLLM шлёт частями параллельно
ENV GUNICORN_WORKERS=3
CMD ["sh", "-c", "exec gunicorn -w \"$GUNICORN_WORKERS\" -b 0.0.0.0:3000 --timeout 120 app:app"]
