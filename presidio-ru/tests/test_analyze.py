"""Проверка распознавания ПДн. Запуск: pytest -q (из каталога presidio-ru)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from recognizers import inn10_ok, inn12_ok, luhn_ok, snils_ok  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return app.test_client()


def found(client, text):
    resp = client.post("/analyze", json={"text": text, "language": "ru"})
    assert resp.status_code == 200
    return {(r["entity_type"], text[r["start"]:r["end"]]) for r in resp.get_json()}


def test_checksums():
    assert inn10_ok("7707083893")
    assert not inn10_ok("7707083894")
    assert inn12_ok("500100732259")
    assert not inn12_ok("500100732250")
    assert snils_ok("11223344595")
    assert not snils_ok("11223344596")
    assert luhn_ok("4111111111111111")


def test_person(client):
    res = found(client, "Договор подписал Иванов Иван Петрович, менеджер отдела продаж.")
    assert ("PERSON", "Иванов Иван Петрович") in res


def test_documents(client):
    text = ("ИНН 500100732259, СНИЛС 112-233-445 95, паспорт серии 45 08 № 123456, "
            "тел. +7 (916) 123-45-67, почта ivanov@example.ru, р/с 40702810900000012345.")
    res = found(client, text)
    assert ("RU_INN_PERSON", "500100732259") in res
    assert ("RU_SNILS", "112-233-445 95") in res
    assert ("RU_PASSPORT", "45 08 № 123456") in res
    assert ("PHONE_NUMBER", "+7 (916) 123-45-67") in res
    assert ("EMAIL_ADDRESS", "ivanov@example.ru") in res
    assert ("RU_BANK_ACCOUNT", "40702810900000012345") in res


def test_card(client):
    assert ("CREDIT_CARD", "4111 1111 1111 1111") in found(client, "Карта 4111 1111 1111 1111 заблокирована.")


def test_no_false_positives(client):
    # суммы, даты, номера документов и ИНН организации (по умолчанию не маскируется) остаются как есть
    text = "Счёт № 1234567890 от 15.10.2026 на сумму 1 250 000,00 руб., ИНН поставщика 7707083893."
    assert found(client, text) == set()


def test_bad_checksum_ignored(client):
    assert found(client, "ИНН 500100732250") == set()


def test_person_normalized(client):
    # один человек в разных падежах — одна начальная форма (по ней обработчик LiteLLM даёт одну метку)
    resp = client.post("/analyze", json={"text": "Письмо для Иванова Ивана Петровича. Иванов Иван Петрович ответил.",
                                         "language": "ru"})
    norms = [r["recognition_metadata"]["normalized"] for r in resp.get_json() if r["entity_type"] == "PERSON"]
    assert norms == ["Иванов Иван Петрович", "Иванов Иван Петрович"]


def test_fio_without_context(client):
    # Natasha такие ФИО без контекста пропускает — ловит FioRecognizer
    assert ("PERSON", "Иванов Иван Петрович") in found(client, "Подписал Иванов Иван Петрович.")
    assert ("PERSON", "Петрова А.С.") in found(client, "Согласовано: Петрова А.С.")
    assert ("PERSON", "И. П. Сидоров") in found(client, "Исполнитель И. П. Сидоров")
    resp = client.post("/analyze", json={"text": "Выдать Петровой Анне Сергеевне", "language": "ru"})
    norms = {r["recognition_metadata"]["normalized"] for r in resp.get_json() if r["entity_type"] == "PERSON"}
    # Natasha ошибается в окончании («Петрова Анне Сергеевна») — сравнение без последней буквы делает pii_guard
    assert any(n.startswith("Петрова Ан") and n.endswith("Сергеевна") for n in norms), norms


def test_fio_no_false_positives(client):
    text = "Ведомость № 12 по отделу «Север». Сдано в Бухгалтерию 20.10.2026, Главная Книга закрыта."
    assert {e for e, _ in found(client, text)} == set()


def test_chinese_pii():
    text = "联系人手机 +86 138 1234 5678，身份证号 11010519491231002X。"
    res = found(client=app.test_client(), text=text)
    assert ("PHONE_NUMBER", "+86 138 1234 5678") in res
    assert ("CN_ID_CARD", "11010519491231002X") in res
    assert {e for e, _ in found(app.test_client(), "订单号 110105194912310021")} == set()   # неверный контрольный знак
