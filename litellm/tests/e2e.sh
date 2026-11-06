#!/usr/bin/env bash
# Проверка pii_guard.py на настоящем LiteLLM без удалённой модели — после правки обработчика или смены версии LiteLLM.
# Запуск на ВМ из /opt/avtom1cgl:  ./litellm/tests/e2e.sh   (presidio-ru должен быть запущен)
# Внутри временного контейнера LiteLLM: поддельная модель (записывает, что ей пришло) + прокси с обработчиком.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=$(docker compose -f ../docker-compose.yml config --images | grep litellm)
docker run --rm --network ai-net -v "$PWD:/t:ro" --entrypoint sh "$IMAGE" -c '
  set -e; mkdir -p /tmp/w && cd /tmp/w && cp /t/pii_guard.py /t/tests/*.py .
  cat > config.yaml <<CFG
model_list:
  - model_name: qwen
    litellm_params: {model: openai/fake, api_base: "http://127.0.0.1:5005/v1", api_key: x}
guardrails:
  - guardrail_name: pii-ru
    litellm_params: {guardrail: pii_guard.PiiRuGuard, mode: [pre_call, post_call], default_on: true}
general_settings: {master_key: sk-test}
CFG
  python -m uvicorn fake_upstream:app --port 5005 >/dev/null 2>&1 &
  PRESIDIO_ANALYZER_API_BASE=http://presidio-analyzer:3000 litellm --config config.yaml --port 4011 >proxy.log 2>&1 &
  for i in $(seq 60); do python -c "import urllib.request as u;u.urlopen(\"http://127.0.0.1:4011/health/liveliness\")" 2>/dev/null && break; sleep 2; done
  python run_e2e.py || { tail -30 proxy.log; exit 1; }'
