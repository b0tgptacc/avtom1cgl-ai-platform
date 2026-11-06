"""Перевод сегментов документа через шлюз LiteLLM на ВМ (маскирование ПДн срабатывает на каждом запросе).
Пакеты по ~4000 символов, ответ — JSON; пропущенные или испорченные сегменты переводятся повторно поодиночке."""
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List

import httpx

from docx_translate import Segment

BASE = os.getenv("LLM_BASE", "http://litellm:4000/v1").rstrip("/")
KEY = os.getenv("LITELLM_MASTER_KEY", "")
MODEL = os.getenv("LLM_MODEL", "qwen-fast")
BATCH_CHARS = int(os.getenv("BATCH_CHARS", "4000"))
BATCH_ITEMS = 40
PARALLEL = int(os.getenv("LLM_PARALLEL", "3"))
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "300"))
EXTRA_TOKENS = int(os.getenv("LLM_EXTRA_TOKENS", "0"))   # запас на рассуждения, если модель «думающая» (qwen)

log = logging.getLogger("doc-translate")

LANGS = {"en": "English", "ru": "Russian", "zh": "Simplified Chinese"}

GLOSSARY = {
    "ru": """Contract style (Russian): "This Agreement" → «настоящий Договор»; "shall" → настоящее время с обязанностью
(«Поставщик обязуется поставить»); "may" → «вправе»; "hereinafter" → «далее — »; "including but not limited to" →
«включая, помимо прочего»; indemnify → возместить потери; representations and warranties → заверения об обстоятельствах
и гарантии; liquidated damages → заранее оценённые убытки (неустойка); governing law → применимое право;
termination for convenience → односторонний отказ без указания причин; force majeure → обстоятельства непреодолимой силы;
甲方/乙方 → Сторона А/Сторона Б; 供方/需方 → Поставщик/Покупатель; 违约金 → неустойка; 仲裁 → арбитраж; 人民币 → юань;
dates «15 октября 2026 г.»; amounts «1 250 000,00».""",
    "en": """Contract style (English): «настоящий Договор» → "this Agreement"; «Стороны» → "the Parties"; «обязуется» → "shall";
«вправе» → "may"; «в лице …, действующего на основании Устава» → "represented by …, acting on the basis of the Charter";
ООО/АО/ПАО → LLC/JSC/PJSC; ИНН/ОГРН → TIN (INN)/OGRN; неустойка → penalty; претензия → claim;
арбитражный суд → state commercial (arbitrazh) court; amounts "RUB 1,250,000.00"; dates "15 October 2026".""",
    "zh": """Contract style (Simplified Chinese): use standard PRC contract terms (甲方/乙方, 违约金, 不可抗力, 争议解决, 适用法律);
Russian company forms: ООО → 有限责任公司; amounts with thousands separators.""",
}

SYSTEM = """You are a professional legal translator. Translate each segment into {lang}.
Rules:
- Return ONLY a JSON object: {{"segments": [{{"id": <id>, "text": "<translation>"}}, ...]}} with exactly the same ids.
- Markers like ⟦1⟧ … ⟦/1⟧ mark formatting (bold, italic): keep every marker, put it around the words that correspond
  to the original marked words; never add, drop or renumber markers.
- Placeholders like <PERSON_1>, <PHONE_NUMBER_2>, <EMAIL_ADDRESS_1> are anonymized data: copy them exactly, unchanged.
- Keep numbers, dates, amounts, clause numbers (1.1, 2.3.4), article references and punctuation of numbering.
- Do not translate company names in quotes; transliterate them if needed. Translate everything else fully.
- One segment = one paragraph or table cell; keep the sentence order. No comments, no explanations.
{glossary}"""


def _chat(messages: list, max_tokens: int) -> str:
    resp = httpx.post(f"{BASE}/chat/completions", timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {KEY}"} if KEY else {},
                      json={"model": MODEL, "messages": messages, "temperature": 0.1,
                            "max_tokens": max_tokens + EXTRA_TOKENS})
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or ""


def _parse(answer: str) -> dict:
    """JSON из ответа: модель иногда оборачивает его в ```json … ``` или добавляет текст вокруг."""
    m = re.search(r"\{.*\}", answer, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return {int(s["id"]): s["text"] for s in data.get("segments", [])
            if isinstance(s, dict) and "id" in s and isinstance(s.get("text"), str)}


def _markers_ok(seg: Segment, text: str) -> bool:
    return sorted(re.findall(r"⟦(\d+)⟧", text)) == sorted(re.findall(r"⟦(\d+)⟧", seg.source))


def _batches(segments: List[Segment]) -> List[List[Segment]]:
    batches, cur, size = [], [], 0
    for s in segments:
        if cur and (size + len(s.source) > BATCH_CHARS or len(cur) >= BATCH_ITEMS):
            batches.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += len(s.source)
    if cur:
        batches.append(cur)
    return batches


def make_translator(target: str) -> Callable[[List[Segment], Callable[[int, int], None]], None]:
    system = SYSTEM.format(lang=LANGS[target], glossary=GLOSSARY.get(target, ""))

    def run_batch(batch: List[Segment]) -> None:
        payload = json.dumps({"segments": [{"id": s.id, "text": s.source} for s in batch]}, ensure_ascii=False)
        try:
            got = _parse(_chat([{"role": "system", "content": system}, {"role": "user", "content": payload}],
                               max_tokens=max(2048, len(payload) * 2)))
        except httpx.HTTPError as e:
            log.warning("batch failed: %s", type(e).__name__)
            got = {}
        for s in batch:
            text = got.get(s.id)
            if text and _markers_ok(s, text):
                s.result = text
        # что не пришло или пришло с испорченными маркерами — повторно, по одному сегменту
        for s in batch:
            if s.result is not None:
                continue
            try:
                one = _parse(_chat([{"role": "system", "content": system},
                                    {"role": "user", "content": json.dumps(
                                        {"segments": [{"id": s.id, "text": s.source}]}, ensure_ascii=False)}],
                                   max_tokens=max(1024, len(s.source) * 3)))
                s.result = one.get(s.id)
            except httpx.HTTPError as e:
                log.warning("segment %d failed: %s", s.id, type(e).__name__)
            if s.result is None:
                s.notes.append("не переведено — оставлен оригинал")

    def translator(segments: List[Segment], progress: Callable[[int, int], None]) -> None:
        batches = _batches(segments)
        done = 0
        progress(0, len(segments))
        with ThreadPoolExecutor(PARALLEL) as pool:
            for batch, _ in zip(batches, pool.map(run_batch, batches)):
                done += len(batch)
                progress(done, len(segments))

    return translator
