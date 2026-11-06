#!/usr/bin/env bash
# Проверка удалённого LiteLLM (наш контур, Qwen) — до установки шлюза и при разборе проблем.
# Запуск с ВМ (или любой машины с доступом):
#   ./check-litellm.sh https://<удалённый-litellm> <ключ> <имя модели> [модель эмбеддингов]
# Нужны curl и python3 (есть в Ubuntu по умолчанию).
set -uo pipefail

BASE=${1:?адрес LiteLLM, например https://llm.domain.local}
KEY=${2:?ключ}
MODEL=${3:?имя модели, например qwen-flash}
EMBED=${4:-}
BASE=${BASE%/}; BASE=${BASE%/v1}

AUTH=(-H "Authorization: Bearer $KEY" -H "Content-Type: application/json")
pretty() { python3 -m json.tool 2>/dev/null || cat; }
step() { echo; echo "=== $1"; }

step "1. Доступность и сертификат"
curl -sS -o /dev/null -w "HTTP %{http_code}, TLS-проверка: %{ssl_verify_result} (0 = сертификат доверенный), время %{time_total} с\n" \
  "$BASE/health/liveliness" || echo "!! нет соединения: сеть, DNS или сертификат (корневой ЦС в /usr/local/share/ca-certificates)"

step "2. Ключ действует, какие модели доступны"
curl -sS "$BASE/v1/models" "${AUTH[@]}" | pretty

step "3. Лимиты ключа (бюджет, модели, срок) — если ключ виртуальный"
curl -sS "$BASE/key/info" "${AUTH[@]}" | pretty

step "4. Перевод без потока (ожидается только перевод, без инструкций и кода)"
curl -sS "$BASE/v1/chat/completions" "${AUTH[@]}" -d @- <<EOF | pretty
{"model": "$MODEL", "temperature": 0.2,
 "messages": [
  {"role": "system", "content": "Ты переводчик. Переведи текст пользователя на английский. Верни только перевод, без пояснений."},
  {"role": "user", "content": "Договор поставки вступает в силу с момента подписания и действует до 31 декабря 2026 года."}]}
EOF

step "5. Тот же запрос БЕЗ системной инструкции (если здесь инструкция по скрипту — дело в настройках модели в LiteLLM)"
curl -sS "$BASE/v1/chat/completions" "${AUTH[@]}" -d @- <<EOF | pretty
{"model": "$MODEL",
 "messages": [{"role": "user", "content": "Переведи на английский: Договор поставки вступает в силу с момента подписания."}]}
EOF

step "6. Потоковый ответ (так работает LibreChat) — должны идти строки data: {...} и в конце data: [DONE]"
curl -sS -N "$BASE/v1/chat/completions" "${AUTH[@]}" -d @- <<EOF | head -n 8
{"model": "$MODEL", "stream": true,
 "messages": [{"role": "user", "content": "Назови три месяца осени."}]}
EOF

step "7. Сохраняет ли модель метки при переводе (важно для маскирования)"
curl -sS "$BASE/v1/chat/completions" "${AUTH[@]}" -d @- <<EOF | pretty
{"model": "$MODEL", "temperature": 0.2,
 "messages": [
  {"role": "system", "content": "Ты переводчик. Переведи на английский. Метки вида <PERSON> и <PHONE_NUMBER> не изменяй. Верни только перевод."},
  {"role": "user", "content": "<PERSON> подписал договор поставки. Его телефон <PHONE_NUMBER>."}]}
EOF

if [[ -n "$EMBED" ]]; then
  step "8. Эмбеддинги (для поиска по файлам в LibreChat)"
  curl -sS "$BASE/v1/embeddings" "${AUTH[@]}" -d "{\"model\": \"$EMBED\", \"input\": \"проверка\"}" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print("размерность:", len(d["data"][0]["embedding"]))' \
    || echo "!! эмбеддинги не работают"
else
  step "8. Эмбеддинги — пропущено (модель не указана 4-м параметром)"
fi
echo
