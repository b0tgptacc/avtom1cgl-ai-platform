#!/usr/bin/env bash
# Проверка цепочки на ВМ: presidio-ru → litellm → удалённый LiteLLM. Запуск из /opt/avtom1cgl.
# curl запускается во временном контейнере в сети ai-net (сервисы наружу не опубликованы).
set -uo pipefail
source .env

net_curl() { docker run --rm --network ai-net curlimages/curl:8.11.1 -sS -m 60 "$@"; }
fail() { echo "!! $1"; echo "   журнал: docker compose logs --tail 30 $2"; exit 1; }

echo "== 1. presidio-ru находит ПДн"
net_curl http://presidio-analyzer:3000/analyze -H 'Content-Type: application/json' \
  -d '{"text":"Иванов Иван Петрович, ИНН 500100732259, тел. +7 916 123-45-67","language":"ru"}' \
  || fail "presidio-ru не отвечает" presidio-analyzer
echo

echo "== 2. LiteLLM жив (ждём до 60 с после запуска)"
for i in $(seq 12); do
  net_curl http://litellm:4000/health/liveliness 2>/dev/null && break
  [[ $i == 12 ]] && fail "LiteLLM не отвечает" litellm
  sleep 5
done
echo

echo "== 3. Перевод через шлюз (в ответе должно быть настоящее ФИО — обратная подстановка)"
net_curl http://litellm:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[
        {"role":"system","content":"Ты переводчик. Переведи текст на английский. Верни только перевод."},
        {"role":"user","content":"Иванов Иван Петрович подписал договор поставки. Его телефон +7 916 123-45-67."}]}' \
  || fail "запрос к LiteLLM не прошёл" litellm
echo

echo "== 4. Два человека в одном чате (ожидается: поставщик — Петров, подписал — Иванов)"
net_curl http://litellm:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen-fast","messages":[
        {"role":"user","content":"Поставщик — Петров Сергей Николаевич."},
        {"role":"assistant","content":"Понял."},
        {"role":"user","content":"Договор подписал Иванов Иван Петрович. Кто поставщик и кто подписал договор? Ответь двумя строками."}]}' \
  || fail "запрос к LiteLLM не прошёл" litellm
echo

echo "== 5. Перевод документов и распознавание отвечают"
net_curl -o /dev/null -w "doc-translate: HTTP %{http_code}\n" http://doc-translate:8000/translate/health || fail "doc-translate не отвечает" doc-translate
net_curl -o /dev/null -w "ocr-ru: HTTP %{http_code}\n" http://ocr-ru:8000/health || fail "ocr-ru не отвечает" ocr-ru
# инструмент агента: без ключа — 401/403/500 от MCP, главное — что сервер отвечает
net_curl -o /dev/null -w "doc-translate-mcp: HTTP %{http_code} (не 000 — сервер отвечает)\n" -X POST http://doc-translate-mcp:8001/mcp || fail "doc-translate-mcp не отвечает" doc-translate-mcp
echo

echo "== 6. Журнал presidio-ru (ожидается: found ['PERSON', 'PHONE_NUMBER'])"
docker compose logs --tail 5 presidio-analyzer
echo
echo "Что наружу ушли метки, а не ФИО, — проверить перехватом трафика (см. pilot-install.md, шаг 4)."
