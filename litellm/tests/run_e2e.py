"""Сценарии проверки pii_guard.py: запускает e2e.sh (или вручную против прокси на :4011 и поддельной модели на :5005)."""
import json, httpx, sys
H = {"Authorization": "Bearer sk-test"}
U = "http://127.0.0.1:4011/v1/chat/completions"
SECRETS = ["Петров", "Иванов", "Иванова", "916", "123-45-67", "500100732259", "ivanov@"]
ok = True
def check(name, cond, info=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"\n     {info}"))
    ok &= bool(cond)

conv = [{"role": "user", "content": "Поставщик — Петров Сергей Николаевич."},
        {"role": "assistant", "content": "Понял, поставщик Петров Сергей Николаевич."},
        {"role": "user", "content": "Договор подписал Иванов Иван Петрович, тел. +7 916 123-45-67. Письмо для Иванова Ивана Петровича."}]

# 1. не поток: сквозная нумерация + падежи + подстановка
open("bodies.jsonl", "w").close()
r = httpx.post(U, headers=H, json={"model": "qwen", "messages": conv}, timeout=60).json()
sent = json.loads(open("bodies.jsonl").read().splitlines()[-1])
users = [m["content"] for m in sent["messages"]]
print("  к модели:", users)
check("Петров и Иванов — разные метки", "<PERSON_1>" in users[0] and "<PERSON_2>" in users[2] and "<PERSON_1>" not in users[2], users)
check("ассистент в истории замаскирован той же меткой", "<PERSON_1>" in users[1], users[1])
check("Иванов в двух падежах — одна метка", users[2].count("<PERSON_2>") == 2, users[2])
content = r["choices"][0]["message"]["content"]
print("  ответ:", content)
check("ответ: Петров вернулся", "Петров Сергей Николаевич" in content, content)
check("ответ: Иванов вернулся (разные падежи → именительный)", "Иванов Иван Петрович" in content and "<PERSON" not in content, content)
check("ответ: телефон вернулся", "+7 916 123-45-67" in content, content)
rc = r["choices"][0]["message"].get("reasoning_content") or ""
check("рассуждение без меток", "<" not in rc and "Иванов" in rc, rc)

# 2. поток
open("bodies.jsonl", "w").close()
text, reas = "", ""
with httpx.stream("POST", U, headers=H, json={"model": "qwen", "messages": conv, "stream": True}, timeout=60) as s:
    for line in s.iter_lines():
        if line.startswith("data: ") and line != "data: [DONE]":
            d = json.loads(line[6:])
            for c in d.get("choices", []):
                text += c.get("delta", {}).get("content") or ""
                reas += c.get("delta", {}).get("reasoning_content") or ""
print("  поток:", text)
check("поток: подстановка при разрезанных метках", "Петров Сергей Николаевич" in text and "Иванов Иван Петрович" in text and "+7 916 123-45-67" in text and "PERSON" not in text, text)
check("поток: рассуждение без меток", "PERSON" not in reas and "PHONE" not in reas, reas)

# 3. в запросах к модели нет настоящих значений (всё тело целиком, включая служебные поля)
raw_all = open("bodies.jsonl", encoding="utf-8").read()
raw_all += "\n".join(json.dumps(json.loads(l), ensure_ascii=False) for l in raw_all.splitlines() if l.strip())
leaks = [s for s in SECRETS if s in raw_all]
check("наружу не ушло ни одного значения", not leaks, leaks)

# 4. запрос без ПДн проходит как есть
r = httpx.post(U, headers=H, json={"model": "qwen", "messages": [{"role": "user", "content": "Назови три месяца осени."}]}, timeout=60).json()
check("без ПДн — без изменений", r["choices"][0]["message"]["content"] == "ОТВЕТ: Назови три месяца осени.", r)

# 5. content списком частей (так шлют некоторые клиенты)
open("bodies.jsonl", "w").close()
r = httpx.post(U, headers=H, json={"model": "qwen", "messages": [{"role": "user", "content": [{"type": "text", "text": "ИНН 500100732259, почта ivanov@example.ru"}]}]}, timeout=60).json()
sent = open("bodies.jsonl").read()
check("части сообщения маскируются", "500100732259" not in sent and "<RU_INN_PERSON_1>" in sent, sent)
check("части: обратная подстановка", "500100732259" in r["choices"][0]["message"]["content"], r)
# 5б. одна женщина в разных падежах — одна метка (Natasha ошибается в «Анне»)
open("bodies.jsonl", "w").close()
httpx.post(U, headers=H, json={"model": "qwen", "messages": [{"role": "user",
           "content": "Выдать Петровой Анне Сергеевне аванс. Петрова Анна Сергеевна получила аванс. Петров Олег Иванович не получил."}]}, timeout=60)
sent = open("bodies.jsonl", encoding="utf-8").read()
check("Петровой Анне / Петрова Анна — одна метка, Петров — другая",
      sent.count("<PERSON_1>") == 2 and "<PERSON_2>" in sent and "<PERSON_3>" not in sent, sent)

# 5в. файл/картинка частью сообщения (мимо маскирования) — отказ, к модели ничего не уходит
open("bodies.jsonl", "w").close()
for part in [{"type": "file", "file": {"filename": "договор.pdf", "file_data": "data:application/pdf;base64,JVBERi0="}},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]:
    r = httpx.post(U, headers=H, json={"model": "qwen", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Что в файле?"}, part]}]}, timeout=60)
    check(f"{part['type']} в сообщении — отказ 400", r.status_code == 400, r.text[:200])
check("к модели ничего не ушло", open("bodies.jsonl").read() == "", "")

# 6. большой текст (~300 тыс. символов): ФИО на стыках частей тоже замаскированы, повтор — из кэша
import time
filler = "Сумма договора 1 250 000 рублей, срок поставки — 30 дней с момента оплаты счёта. "
names = ["Сидоров Пётр Ильич", "Кузнецова Мария Андреевна", "Смирнов Олег Викторович", "Попова Елена Игоревна"]
big = "".join(filler * 40 + f"Ответственный — {names[i % 4]}, тел. +7 916 555-{i:02d}-{i:02d}.\n"
              for i in range(60))[:300000]
big = big + " Подписал Иванов Иван Петрович."
open("bodies.jsonl", "w").close()
t = time.time()
r = httpx.post(U, headers=H, json={"model": "qwen", "messages": [{"role": "user", "content": big}]}, timeout=300).json()
t1 = time.time() - t
sent = open("bodies.jsonl", encoding="utf-8").read()
check(f"большой текст: {len(big)} символов за {t1:.1f} с, наружу без ФИО и телефонов",
      not [x for x in ["Иванов", "555-", "Сидоров", "Кузнецова", "Смирнов", "Попова"] if x in sent], sent[-300:])
check("большой текст: 5 человек — 5 меток", sent.count("<PERSON_5>") >= 1 and "<PERSON_6>" not in sent, "")
check("большой текст: подстановка", "Иванов Иван Петрович" in r["choices"][0]["message"]["content"], "")
t = time.time()
httpx.post(U, headers=H, json={"model": "qwen", "messages": [{"role": "user", "content": big},
           {"role": "assistant", "content": "Понял."}, {"role": "user", "content": "Кто подписал?"}]}, timeout=300)
t2 = time.time() - t
check(f"большой текст в истории: повторно не распознаётся ({t2:.1f} с против {t1:.1f} с)", t2 < t1 / 3, "")
# 7. агент: вызов инструмента — аргументы с настоящими данными для инструмента, наружу только метки
tool_q = [{"role": "user", "content": "ВЫЗОВ: запиши, что договор подписал Иванов Иван Петрович, тел. +7 916 123-45-67"}]
open("bodies.jsonl", "w").close()
r = httpx.post(U, headers=H, json={"model": "qwen", "messages": tool_q}, timeout=60).json()
args = r["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
check("инструмент: аргументы с настоящими значениями", "Иванов Иван Петрович" in args and "+7 916 123-45-67" in args and "PERSON" not in args, args)
args = ""
with httpx.stream("POST", U, headers=H, json={"model": "qwen", "messages": tool_q, "stream": True}, timeout=60) as s:
    for line in s.iter_lines():
        if line.startswith("data: ") and line != "data: [DONE]":
            for c in json.loads(line[6:]).get("choices", []):
                for tc in c.get("delta", {}).get("tool_calls") or []:
                    args += (tc.get("function") or {}).get("arguments") or ""
check("инструмент, поток: аргументы собраны и подставлены", "Иванов Иван Петрович" in args and "+7 916 123-45-67" in args and "PERSON" not in args and json.loads(args), args)
# история агента: прошлый вызов с настоящими данными в аргументах и результат инструмента
hist = tool_q + [{"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                  "function": {"name": "write_file", "arguments": json.dumps({"text": "Иванов Иван Петрович, +7 916 123-45-67"}, ensure_ascii=False)}}]},
                 {"role": "tool", "tool_call_id": "call_1", "content": "Записано: Иванов Иван Петрович"},
                 {"role": "user", "content": "Что записано?"}]
open("bodies.jsonl", "w").close()
httpx.post(U, headers=H, json={"model": "qwen", "messages": hist}, timeout=60)
sent = open("bodies.jsonl", encoding="utf-8").read()
sent += json.dumps(json.loads(sent.splitlines()[-1]), ensure_ascii=False)
leaks = [x for x in ("Иванов", "916", "123-45-67") if x in sent]
check("история агента: аргументы и результаты инструментов замаскированы", not leaks and "<PERSON_1>" in sent, leaks or sent[:300])

sys.exit(0 if ok else 1)
