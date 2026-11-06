import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Переведи приложенный договор на английский с сохранением таблиц", "translation"),
        ("Извлеки из договора срок оплаты, валюту и штрафы", "extraction"),
        ("Обезличь этот документ перед передачей в облако", "anonymization"),
        ("Рассчитай стоимость партии с НДС и скидкой", "calculation"),
        ("Создай коммерческое предложение в DOCX", "document_creation"),
        ("Объясни, что такое инкотермс", "general"),
    ],
)
def test_profile_for_user_request(text, expected):
    from profiles import profile_for_messages

    assert profile_for_messages([{"role": "user", "content": text}]) == expected


def test_profile_policy_is_injected_without_discarding_user_request():
    from profiles import apply_profile_policy

    body = {
        "messages": [
            {"role": "system", "content": "base policy"},
            {"role": "user", "content": "Рассчитай сумму счёта"},
        ]
    }
    result, profile = apply_profile_policy(body)

    assert profile == "calculation"
    assert result["messages"][-1]["role"] == "developer"
    assert "CALCULATION" in result["messages"][-1]["content"]
    assert any(m.get("content") == "Рассчитай сумму счёта" for m in result["messages"])


def test_unknown_request_has_safe_general_profile():
    from profiles import apply_profile_policy

    result, profile = apply_profile_policy({"messages": [{"role": "user", "content": "Сделай необычную задачу"}]})

    assert profile == "general"
    assert "GENERAL" in result["messages"][-1]["content"]
