"""Маскирование ПДн для LiteLLM со сквозной нумерацией меток по всему чату (замена встроенного guardrail presidio).

Почему свой обработчик: встроенный presidio в LiteLLM нумерует метки заново в каждом сообщении, и два разных
человека из разных сообщений получают одну метку <PERSON_1> — модель их путает, обратная подстановка
возвращает одно ФИО за двоих.

Здесь:
- одна таблица меток на весь запрос (вся история чата): Петров → <PERSON_1>, Иванов → <PERSON_2>;
- один и тот же человек в разных падежах — одна метка (начальная форма из presidio-ru, recognition_metadata.normalized);
  в ответ подставляется самая короткая из форм, встреченных в чате (обычно именительный падеж);
- однофамильцы с одинаковым ФИО получают одну метку — подстановка всё равно верна (строка та же),
  различает их модель по контексту (организация, должность), как и человек;
- в ответе метки заменяются обратно, в том числе в потоковом ответе и в рассуждениях модели;
- presidio-ru недоступен → запрос не уходит к модели (503), без маскирования ничего наружу не отправляем;
- файлы, картинки, аудио в сообщении (не текст) → отказ 400: их содержимое не маскируется. LibreChat настроен
  отдавать файлы текстом (fileConfig в librechat.yaml); отказ здесь — страховка, если настройка собьётся;
- большие тексты (окно до 270 тыс. токенов ≈ 1 млн символов) делятся на части по абзацам и распознаются
  параллельно; результаты кэшируются в памяти — история чата при каждом новом сообщении заново не распознаётся.

Подключение (config.yaml):
  guardrails:
    - guardrail_name: pii-ru
      litellm_params:
        guardrail: pii_guard.PiiRuGuard
        mode: [pre_call, post_call]
        default_on: true
"""
import asyncio
import copy
import hashlib
import os
import re
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException
from litellm._logging import verbose_proxy_logger as log
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.types.utils import ChatCompletionDeltaToolCall, Function

ANALYZER = os.getenv("PRESIDIO_ANALYZER_API_BASE", "http://presidio-analyzer:3000").rstrip("/")
TIMEOUT = float(os.getenv("PII_GUARD_TIMEOUT", "30"))
MAP_TTL = 600            # секунд: таблица меток запроса живёт до ответа, остатки (оборванные запросы) чистятся
CHUNK = int(os.getenv("PII_GUARD_CHUNK", "20000"))          # символов в одном запросе к presidio-ru
PARALLEL = int(os.getenv("PII_GUARD_PARALLEL", "3"))        # одновременных запросов (= процессов presidio-ru)
CACHE_TTL = int(os.getenv("PII_GUARD_CACHE_TTL", "7200"))   # секунд: кэш распознавания (только в памяти процесса)
CACHE_MAX = 2000                                            # записей в кэше
TEXT_PART_TYPES = {"text", "input_text"}                    # остальное (image_url, file, input_audio…) не маскируется
HOLD_MAX = 40            # потоковый ответ: сколько символов на конце придерживать, пока метка не придёт целиком

# хвост потока, который может оказаться началом метки: <PERSON_1> или PERSON_1 без скобок
_TAIL = re.compile(r"[<A-Z0-9_>]+$")


# --- маскирование ---------------------------------------------------------------------------------

def _key(entity: str, text: str, meta: Optional[dict]) -> Tuple[str, str]:
    """Ключ «то же самое значение»: ФИО — по начальной форме, номера — по цифрам, остальное — без регистра."""
    if entity == "PERSON":
        # начальная форма от presidio-ru без конечных гласных: Natasha ошибается в окончаниях и роде
        # («Петрова Анна» → «Петров Анна», «Анне» вместо «Анна»). Мужчину и женщину с одной фамилией
        # различают имя и отчество («сергеевич» / «сергеевн»); не различит только Петров А.С. и Петрова А.С.
        words = ((meta or {}).get("normalized") or text).lower().replace("ё", "е").split()
        value = " ".join(re.sub(r"(?<=\w\w\w)[аеиоуыэюяйь]+$", "", w) for w in words)
    elif entity in ("PHONE_NUMBER",):
        value = re.sub(r"\D", "", text)[-10:]
    elif entity.startswith("RU_") or entity == "CREDIT_CARD":
        value = re.sub(r"\D", "", text)
    else:
        value = text
    return entity, " ".join(value.split()).lower()


def _split(text: str) -> List[Tuple[int, str]]:
    """Части не длиннее CHUNK: режем по абзацу, иначе по концу предложения, иначе по пробелу —
    чтобы не разрезать ФИО или номер. Возвращает (смещение, часть)."""
    parts, start = [], 0
    while len(text) - start > CHUNK:
        window = text[start:start + CHUNK]
        cut = max(window.rfind("\n"), window.rfind(". "), CHUNK // 2)
        if cut == CHUNK // 2:   # ни абзаца, ни точки во второй половине окна
            cut = max(window.rfind(" "), CHUNK // 2)
        parts.append((start, text[start:start + cut + 1]))
        start += cut + 1
    parts.append((start, text[start:]))
    return parts


def _pick(results: List[dict]) -> List[dict]:
    """Убрать пересечения: при наложении остаётся более длинный фрагмент."""
    chosen: List[dict] = []
    for r in sorted(results, key=lambda r: (r["start"], -(r["end"] - r["start"]))):
        if chosen and r["start"] < chosen[-1]["end"]:
            continue
        chosen.append(r)
    return chosen


class TokenTable:
    """Метки одного запроса: значение → метка, метка → что вернуть пользователю."""

    def __init__(self) -> None:
        self.by_key: Dict[Tuple[str, str], str] = {}
        self.forms: Dict[str, List[str]] = {}     # метка → встреченные формы (для обратной подстановки)
        self.counters: Dict[str, int] = {}

    def token(self, entity: str, text: str, meta: Optional[dict]) -> str:
        key = _key(entity, text, meta)
        tok = self.by_key.get(key)
        if tok is None:
            self.counters[entity] = self.counters.get(entity, 0) + 1
            tok = f"<{entity}_{self.counters[entity]}>"
            self.by_key[key] = tok
            self.forms[tok] = []
        if text not in self.forms[tok]:
            self.forms[tok].append(text)
        return tok

    def mask(self, text: str, results: List[dict]) -> str:
        chosen = _pick(results)
        # метки назначаем слева направо (первое упоминание — меньший номер), заменяем справа налево
        tokens = [self.token(r["entity_type"], text[r["start"]:r["end"]], r.get("recognition_metadata"))
                  for r in chosen]
        for r, tok in reversed(list(zip(chosen, tokens))):
            text = text[:r["start"]] + tok + text[r["end"]:]
        return text

    def value(self, tok: str) -> str:
        """Что вернуть пользователю: форму из его же текста. Разные падежи — самую короткую,
        обычно это именительный («Петрова Анна Сергеевна» короче «Петровой Анне Сергеевне»)."""
        return min(self.forms[tok], key=len)

    def unmask(self, text: str) -> str:
        if not text or not self.forms:
            return text
        # модель иногда теряет угловые скобки: принимаем и <PERSON_1>, и PERSON_1
        def repl(m: re.Match) -> str:
            tok = f"<{m.group(1)}>"
            return self.value(tok) if tok in self.forms else m.group(0)
        return re.sub(r"<?\b([A-Z][A-Z_]*?_\d+)\b>?", repl, text)


# --- обработчик LiteLLM ---------------------------------------------------------------------------

class PiiRuGuard(CustomGuardrail):

    def __init__(self, **kwargs: Any) -> None:
        self._tables: Dict[str, Tuple[float, TokenTable]] = {}
        self._cache: Dict[str, Tuple[float, List[dict]]] = {}
        self._sem: Optional[asyncio.Semaphore] = None
        super().__init__(**kwargs)

    # таблица меток живёт между pre_call и post_call в памяти процесса; в запрос к модели она не попадает
    def _put(self, data: dict, table: TokenTable) -> None:
        now = time.time()
        for k in [k for k, (t, _) in self._tables.items() if now - t > MAP_TTL]:
            self._tables.pop(k, None)
        call_id = data.setdefault("litellm_call_id", str(uuid.uuid4()))
        self._tables[call_id] = (now, table)

    def _get(self, data: Optional[dict], pop: bool = True) -> Optional[TokenTable]:
        call_id = (data or {}).get("litellm_call_id")
        entry = (self._tables.pop if pop else self._tables.get)(call_id, None) if call_id else None
        return entry[1] if entry else None

    async def _analyze_part(self, client: httpx.AsyncClient, offset: int, text: str) -> List[dict]:
        if self._sem is None:
            self._sem = asyncio.Semaphore(PARALLEL)
        async with self._sem:
            resp = await client.post(f"{ANALYZER}/analyze", json={"text": text, "language": "ru"})
        resp.raise_for_status()
        found = resp.json()
        for r in found:
            r["start"] += offset
            r["end"] += offset
        return found

    async def _analyze(self, client: httpx.AsyncClient, text: str) -> List[dict]:
        key = hashlib.sha256(text.encode()).hexdigest()
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return copy.deepcopy(hit[1])
        chunks = await asyncio.gather(*(self._analyze_part(client, o, t) for o, t in _split(text)))
        found = [r for part in chunks for r in part]
        if len(self._cache) >= CACHE_MAX:   # выбросить устаревшие, а если их нет — самые старые
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:CACHE_MAX // 4]:
                self._cache.pop(k, None)
        self._cache[key] = (now, found)
        return copy.deepcopy(found)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type) -> dict:
        messages = data.get("messages")
        if not messages:
            return data

        # все текстовые части всех сообщений: (сообщение, индекс части или None для строки)
        parts: List[Tuple[dict, Optional[int], str]] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str) and content:
                parts.append((m, None, content))
            elif isinstance(content, list):
                for i, c in enumerate(content):
                    if not isinstance(c, dict):
                        continue
                    if c.get("type", "text") not in TEXT_PART_TYPES:
                        log.warning("pii-ru: отклонена нетекстовая часть сообщения: %s", c.get("type"))
                        raise HTTPException(status_code=400, detail=(
                            "Файлы и изображения передаются модели только как текст (через распознавание) — "
                            "иначе их содержимое не проходит маскирование ПДн"))
                    if isinstance(c.get("text"), str) and c["text"]:
                        parts.append((m, i, c["text"]))
            # агент: аргументы вызовов инструментов в истории содержат уже подставленные настоящие значения
            for j, call in enumerate(m.get("tool_calls") or []):
                args = (call.get("function") or {}).get("arguments") if isinstance(call, dict) else None
                if isinstance(args, str) and args:
                    parts.append((m, ("tool", j), args))
        if not parts:
            return data

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                results = await asyncio.gather(*(self._analyze(client, t) for _, _, t in parts))
        except Exception as e:  # без маскирования запрос не отправляем
            log.error("pii-ru: presidio-ru недоступен: %s", type(e).__name__)
            raise HTTPException(status_code=503, detail="Маскирование ПДн недоступно, запрос не отправлен")

        table = TokenTable()
        for (m, i, text), found in zip(parts, results):   # по порядку сообщений — сквозная нумерация
            masked = table.mask(text, found) if found else text
            if i is None:
                m["content"] = masked
            elif isinstance(i, tuple):
                m["tool_calls"][i[1]]["function"]["arguments"] = masked
            else:
                m["content"][i]["text"] = masked
        if table.forms:
            self._put(data, table)
        # в журнал — только количество, без значений
        log.info("pii-ru: masked %s", {t: n for t, n in table.counters.items()})
        return data

    async def async_post_call_success_hook(self, data: dict, user_api_key_dict, response: Any) -> Any:
        table = self._get(data)
        if table is None:
            return response
        for choice in getattr(response, "choices", None) or []:
            msg = getattr(choice, "message", None)
            if msg is None:
                continue
            for attr in ("content", "reasoning_content"):
                val = getattr(msg, attr, None)
                if isinstance(val, str):
                    setattr(msg, attr, table.unmask(val))
            psf = getattr(msg, "provider_specific_fields", None)
            if isinstance(psf, dict) and isinstance(psf.get("reasoning"), str):
                psf["reasoning"] = table.unmask(psf["reasoning"])
            for call in getattr(msg, "tool_calls", None) or []:
                fn = getattr(call, "function", None)
                if fn is not None and isinstance(getattr(fn, "arguments", None), str):
                    fn.arguments = table.unmask(fn.arguments)
        return response

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response: Any, request_data: dict
    ) -> AsyncGenerator[Any, None]:
        table = self._get(request_data)
        if table is None:
            async for chunk in response:
                yield chunk
            return

        held: Dict[Tuple[int, str], str] = {}   # (choice, поле) → придержанный хвост
        last = None

        def emit(idx: int, attr: str, text: str, final: bool) -> str:
            buf = held.pop((idx, attr), "") + text
            if not final:
                m = _TAIL.search(buf)
                if m and len(buf) - m.start() <= HOLD_MAX:
                    held[(idx, attr)] = buf[m.start():]
                    buf = buf[:m.start()]
            return table.unmask(buf)

        def _tool_tails(idx: int) -> list:
            out = []
            for key in [k for k in held if k[0] == idx and k[1].startswith("tool")]:
                out.append(ChatCompletionDeltaToolCall(
                    index=int(key[1][4:]), function=Function(arguments=table.unmask(held.pop(key)), name=None)))
            return out

        async for chunk in response:
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                final = getattr(choice, "finish_reason", None) is not None
                for attr in ("content", "reasoning_content"):
                    val = getattr(delta, attr, None)
                    if isinstance(val, str) or (final and (choice.index, attr) in held):
                        setattr(delta, attr, emit(choice.index, attr, val or "", final))
                calls = getattr(delta, "tool_calls", None) or []
                for call in calls:
                    fn = getattr(call, "function", None)
                    if fn is not None and isinstance(getattr(fn, "arguments", None), str):
                        fn.arguments = emit(choice.index, f"tool{call.index}", fn.arguments, final)
                if final:                      # хвосты аргументов, придержанные до конца, — отдельной частью
                    rest = _tool_tails(choice.index)
                    if rest:
                        delta.tool_calls = list(calls) + rest
            last = chunk
            yield chunk

        if held and last is not None:   # поток оборвался без finish_reason — отдать остаток
            tail = copy.deepcopy(last)
            for choice in tail.choices:
                for attr in ("content", "reasoning_content"):
                    if hasattr(choice.delta, attr):
                        setattr(choice.delta, attr, table.unmask(held.pop((choice.index, attr), "")) or None)
                choice.delta.tool_calls = _tool_tails(choice.index) or None
            yield tail
