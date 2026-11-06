"""Presidio Analyzer для русского языка — совместим с REST API официального образа (/analyze, /health, /supportedentities).
Вызывается LiteLLM (guardrail presidio) по адресу PRESIDIO_ANALYZER_API_BASE."""
import logging
import os

from flask import Flask, jsonify, request
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider

from recognizers import LANG, build_recognizers

# Какие сущности маскировать. LOCATION и ORGANIZATION по умолчанию выключены: города и названия компаний
# обычно нужны модели для работы и не являются ПДн сами по себе.
DEFAULT_ENTITIES = ("PERSON,RU_INN_PERSON,RU_SNILS,RU_PASSPORT,PHONE_NUMBER,"
                    "EMAIL_ADDRESS,RU_BANK_ACCOUNT,CREDIT_CARD,CN_ID_CARD")
MASK_ENTITIES = [e.strip() for e in os.getenv("MASK_ENTITIES", DEFAULT_ENTITIES).split(",") if e.strip()]
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "0.5"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("presidio-ru")


def build_engine() -> AnalyzerEngine:
    nlp = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": LANG, "model_name": "ru_core_news_sm"}],
    }).create_engine()
    registry = RecognizerRegistry(supported_languages=[LANG])
    for recognizer in build_recognizers():
        registry.add_recognizer(recognizer)
    return AnalyzerEngine(registry=registry, nlp_engine=nlp, supported_languages=[LANG])


engine = build_engine()
app = Flask(__name__)


@app.get("/health")
def health():
    return "Presidio Analyzer service is up"


@app.get("/supportedentities")
def supported_entities():
    return jsonify(MASK_ENTITIES)


@app.post("/analyze")
def analyze():
    body = request.get_json(force=True) or {}
    text = body.get("text") or ""
    if not text:
        return jsonify([])
    asked = body.get("entities")
    entities = [e for e in asked if e in MASK_ENTITIES] if asked else MASK_ENTITIES
    # язык из запроса не используем: сервис только русский, а распознаватели шаблонов срабатывают и в смешанном тексте
    results = engine.analyze(text=text, language=LANG, entities=entities,
                             score_threshold=body.get("score_threshold", SCORE_THRESHOLD))
    # сам текст в журнал не пишем — только число и типы находок
    log.info("analyze: %d chars, found %s", len(text), sorted({r.entity_type for r in results}))
    return jsonify([r.to_dict() for r in results])
