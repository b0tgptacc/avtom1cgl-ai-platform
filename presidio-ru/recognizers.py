"""Распознаватели ПДн для русского текста: Natasha (ФИО, адреса, организации) + шаблоны с проверкой контрольных сумм."""
import re
from functools import lru_cache
from typing import Callable, List, Optional

from natasha import Doc, MorphVocab, NewsEmbedding, NewsMorphTagger, NewsNERTagger, Segmenter
from natasha.norm import normalize
from presidio_analyzer import EntityRecognizer, RecognizerResult

LANG = "ru"


@lru_cache(maxsize=1)
def _natasha():
    """Модели Natasha — одни на все распознаватели процесса."""
    emb = NewsEmbedding()
    return Segmenter(), NewsNERTagger(emb), NewsMorphTagger(emb), MorphVocab()




class NatashaRecognizer(EntityRecognizer):
    """Именованные сущности через Natasha: PER → PERSON, LOC → LOCATION, ORG → ORGANIZATION.
    В recognition_metadata.normalized — начальная форма («Иванова Ивана» → «Иванов Иван»): по ней обработчик
    маскирования в LiteLLM узнаёт одного и того же человека в разных падежах и даёт ему одну метку."""

    TYPES = {"PER": "PERSON", "LOC": "LOCATION", "ORG": "ORGANIZATION"}

    def __init__(self, score: float = 0.85):
        self.score = score
        super().__init__(supported_entities=list(self.TYPES.values()),
                         supported_language=LANG, name="NatashaRecognizer")

    def load(self) -> None:
        self.segmenter, self.tagger, self.morph, self.vocab = _natasha()

    def analyze(self, text: str, entities: List[str], nlp_artifacts=None) -> List[RecognizerResult]:
        doc = Doc(text)
        doc.segment(self.segmenter)
        doc.tag_ner(self.tagger)
        doc.tag_morph(self.morph)
        results = []
        for span in doc.spans:
            entity = self.TYPES.get(span.type)
            if entity and entity in entities:
                span.normalize(self.vocab)
                results.append(RecognizerResult(entity, span.start, span.stop, self.score,
                                                recognition_metadata={"normalized": span.normal or span.text}))
        return results


class FioRecognizer(EntityRecognizer):
    """Страховка к Natasha: ФИО по форме — с отчеством (Иванов Иван Петрович, Иван Петрович Иванов)
    и с инициалами (Иванов И.П., И. П. Иванов). Natasha пропускает ФИО без контекста,
    например в начале фразы «Подписал Иванов Иван Петрович.». Регистр букв важен — без IGNORECASE."""

    W = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"                                  # слово с заглавной
    P = r"[А-ЯЁ][а-яё]+(?:ович|евич|ич|овн|евн|ичн)(?:а|у|ем|ом|е|ы|ой)?"    # отчество в любом падеже
    I = r"[А-ЯЁ]\.\s?[А-ЯЁ]\."                                              # инициалы
    # оценки ниже, чем у Natasha (0.85): где нашли оба, остаётся результат Natasha — её начальная форма точнее
    PATTERNS = [
        (rf"\b{W}\s+{W}\s+{P}\b", 0.8),      # Фамилия Имя Отчество
        (rf"\b{W}\s+{P}\s+{W}\b", 0.8),      # Имя Отчество Фамилия
        (rf"\b{W}\s+{I}", 0.7),               # Фамилия И.О.
        (rf"(?<![А-ЯЁа-яё]){I}\s?{W}\b", 0.7),  # И.О. Фамилия
    ]

    def __init__(self):
        self.regexes = [(re.compile(p), score) for p, score in self.PATTERNS]
        super().__init__(supported_entities=["PERSON"], supported_language=LANG, name="FioRecognizer")

    def load(self) -> None:
        pass

    def analyze(self, text: str, entities: List[str], nlp_artifacts=None) -> List[RecognizerResult]:
        if "PERSON" not in entities:
            return []
        matches = [(m, score) for regex, score in self.regexes for m in regex.finditer(text)]
        if not matches:
            return []
        # падеж определяется по всему тексту: без контекста «Анне» в «Петровой Анне Сергеевне» не угадать
        segmenter, _, morph, vocab = _natasha()
        doc = Doc(text)
        doc.segment(segmenter)
        doc.tag_morph(morph)
        results = []
        for m, score in matches:
            tokens = [t for t in doc.tokens if t.start >= m.start() and t.stop <= m.end()]
            normal = (normalize(vocab, tokens) if tokens else "") or m.group(0)
            results.append(RecognizerResult("PERSON", m.start(), m.end(), score,
                                            recognition_metadata={"normalized": normal}))
        return results


class RegexRecognizer(EntityRecognizer):
    """Шаблон с группой (?P<v>...) — маскируется только она (ключевое слово «паспорт», «ИНН» остаётся в тексте).
    validator получает строку из одних цифр и отсекает случайные совпадения."""

    def __init__(self, entity: str, pattern: str, score: float,
                 validator: Optional[Callable[[str], bool]] = None, name: Optional[str] = None):
        self.regex = re.compile(pattern, re.IGNORECASE)
        self.score = score
        self.validator = validator
        super().__init__(supported_entities=[entity], supported_language=LANG,
                         name=name or f"{entity}_Recognizer")

    def load(self) -> None:
        pass

    def analyze(self, text: str, entities: List[str], nlp_artifacts=None) -> List[RecognizerResult]:
        entity = self.supported_entities[0]
        if entity not in entities:
            return []
        results = []
        for m in self.regex.finditer(text):
            value = m.group("v")
            if self.validator and not self.validator(re.sub(r"[^\dXx]", "", value).upper()):
                continue
            results.append(RecognizerResult(entity, m.start("v"), m.end("v"), self.score))
        return results


# --- контрольные суммы -------------------------------------------------------

def _weighted(digits: str, weights) -> int:
    return sum(int(d) * w for d, w in zip(digits, weights)) % 11 % 10


def inn10_ok(d: str) -> bool:
    return len(d) == 10 and _weighted(d, [2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(d[9])


def inn12_ok(d: str) -> bool:
    return (len(d) == 12
            and _weighted(d, [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(d[10])
            and _weighted(d, [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(d[11]))


def snils_ok(d: str) -> bool:
    if len(d) != 11:
        return False
    s = sum(int(x) * (9 - i) for i, x in enumerate(d[:9]))
    check = s % 101
    if check == 100:
        check = 0
    return check == int(d[9:])


def cn_id_ok(d: str) -> bool:
    """Удостоверение личности КНР: 18 знаков, последний — контрольный (ISO 7064 MOD 11-2, может быть X)."""
    if len(d) != 18 or not d[:17].isdigit():
        return False
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    return "10X98765432"[sum(int(x) * w for x, w in zip(d, weights)) % 11] == d[17]


def luhn_ok(d: str) -> bool:
    total = 0
    for i, x in enumerate(reversed(d)):
        n = int(x) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return len(d) in (16, 18, 19) and total % 10 == 0


# --- набор распознавателей ---------------------------------------------------

def build_recognizers() -> List[EntityRecognizer]:
    nd = r"(?<!\d)"   # слева не цифра
    na = r"(?!\d)"    # справа не цифра
    return [
        NatashaRecognizer(),
        FioRecognizer(),
        # ИНН физлица/ИП — 12 цифр, организации — 10 цифр; без верной контрольной суммы не считаем ИНН
        RegexRecognizer("RU_INN_PERSON", nd + r"(?P<v>\d{12})" + na, 0.9, inn12_ok),
        RegexRecognizer("RU_INN_ORG", nd + r"(?P<v>\d{10})" + na, 0.9, inn10_ok),
        RegexRecognizer("RU_SNILS", nd + r"(?P<v>\d{3}-?\d{3}-?\d{3}[ -]?\d{2})" + na, 0.9, snils_ok),
        # паспорт РФ — только рядом со словом «паспорт», иначе 10 цифр ловятся где попало
        RegexRecognizer("RU_PASSPORT",
                        r"паспорт\w*[^\d\n]{0,40}?(?P<v>\d{2}\s?\d{2}\s?(?:№\s?)?\d{6})" + na, 0.9),
        RegexRecognizer("PHONE_NUMBER",
                        nd + r"(?P<v>(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2})" + na, 0.8),
        # Китай: мобильный +86 1xx xxxx xxxx и удостоверение личности (контрольный знак). Китайские ФИО не распознаются
        RegexRecognizer("PHONE_NUMBER", nd + r"(?P<v>(?:\+86|0086)[\s-]?1[3-9]\d[\s-]?\d{4}[\s-]?\d{4})" + na, 0.8,
                        name="PHONE_NUMBER_CN_Recognizer"),
        RegexRecognizer("CN_ID_CARD", nd + r"(?P<v>\d{17}[\dXx])" + na, 0.9, cn_id_ok),
        RegexRecognizer("EMAIL_ADDRESS", r"(?P<v>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)", 0.9),
        # расчётный / лицевой счёт — 20 цифр подряд
        RegexRecognizer("RU_BANK_ACCOUNT", nd + r"(?P<v>\d{20})" + na, 0.7),
        RegexRecognizer("CREDIT_CARD", nd + r"(?P<v>\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}(?:\d{2,3})?)" + na,
                        0.9, luhn_ok),
    ]
