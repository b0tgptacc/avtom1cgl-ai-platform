FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py pool.py ./
COPY tests ./tests
RUN python -m pytest -q -p no:cacheprovider tests
RUN useradd --system --uid 10005 keypool
USER keypool
EXPOSE 8081
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port 8081 --timeout-keep-alive 75"]
