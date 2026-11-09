FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
COPY quality-tools/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && useradd --create-home --uid 10001 quality
COPY calculator /srv/calculator
COPY document-evidence /srv/document-evidence
COPY translation-qa /srv/translation-qa
COPY anonymization-audit /srv/anonymization-audit
COPY document-compose /srv/document-compose
COPY quality-tools /srv/quality-tools
RUN mkdir -p /uploads /work && chown -R quality:quality /srv /uploads /work

USER quality
WORKDIR /srv/quality-tools
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
