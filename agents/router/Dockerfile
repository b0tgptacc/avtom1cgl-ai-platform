FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py profiles.py ./
COPY tests ./tests
RUN python -m pytest -q -p no:cacheprovider tests
RUN useradd --system --uid 10004 router
USER router
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port 8080 --proxy-headers --timeout-keep-alive 75"]
