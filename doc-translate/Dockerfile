FROM python:3.12-slim

# LibreOffice без интерфейса — DOC/RTF/ODT → DOCX и DOCX → PDF; шрифты, совместимые по метрике с Times/Arial/Calibri,
# и шрифт с иероглифами — чтобы PDF не «поехал» после перевода
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui fonts-liberation2 fonts-dejavu-core fonts-crosextra-carlito \
        fonts-crosextra-caladea fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py convert.py docx_translate.py llm.py mcp_server.py pdf_translate.py page.html ./
COPY tests ./tests

# прогон тестов при сборке (вёрстка, повторы запросов, вход по сессии, DOCX → PDF, инструмент агента по MCP)
RUN HOME=/tmp JOBS_DIR=/tmp/dt-test LIBRECHAT_UPLOADS=/tmp/dt-uploads python -m pytest -q -p no:cacheprovider tests && rm -rf /tmp/dt-test /tmp/dt-uploads /tmp/.config

RUN useradd --system --uid 10003 --create-home dt && mkdir -p /tmp/jobs && chown dt /tmp/jobs
USER dt
ENV HOME=/home/dt

EXPOSE 8000
# один процесс (задания в памяти), потоки — для одновременных пользователей
CMD ["sh", "-c", "exec gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 --timeout 900 app:app"]
