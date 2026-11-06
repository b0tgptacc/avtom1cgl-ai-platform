#!/usr/bin/env bash
# Обновление ВМ из нового архива ветки (папка deploy). Ваши значения не трогает: имена моделей в litellm/config.yaml,
# server_name и сертификаты nginx, .env, version в librechat.yaml. Перед изменениями — резервная копия.
#   ./apply-update.sh ~/deploy-new/deploy
set -euo pipefail

N=$(realpath "${1:?папка deploy из нового архива}")
GW=/opt/avtom1cgl
LC=/opt/librechat
[[ -f "$N/docker-compose.yml" && -d "$N/ocr-ru" ]] || { echo "!! в $N нет docker-compose.yml и ocr-ru — укажите папку deploy"; exit 1; }
step() { echo; echo "=== $1"; }

step "Резервная копия перед обновлением"
if sudo test -x /usr/local/sbin/avtom1cgl-backup.sh; then sudo /usr/local/sbin/avtom1cgl-backup.sh | tail -1
else echo "!! backup не установлен (шаг 8) — продолжаю без копии"; fi
STAMP=$(date +%F_%H%M)
cp "$GW/docker-compose.yml" "$GW/docker-compose.yml.bak-$STAMP"
cp "$GW/litellm/config.yaml" "$GW/litellm/config.yaml.bak-$STAMP"

step "Код шлюза: presidio-ru, ocr-ru, обработчик маскирования, скрипты"
find "$N" -type f \( -name '*.py' -o -name '*.sh' -o -name '*.yml' -o -name '*.yaml' -o -name '*.txt' -o -name '*.md' \
     -o -name '*.conf' -o -name 'Dockerfile' -o -name '.env.example' \) -exec sed -i 's/\r$//' {} +
rm -rf "$GW/ocr-ru" "$GW/doc-translate" "$GW/agents"
cp -r "$N/presidio-ru/." "$GW/presidio-ru/"
cp -r "$N/ocr-ru" "$N/doc-translate" "$N/agents" "$GW/"
mkdir -p "$GW/librechat" && rm -rf "$GW/librechat/skill" && cp -r "$N/librechat/skill" "$GW/librechat/"   # общие навыки стенда агентов
cp "$N/litellm/pii_guard.py" "$GW/litellm/"
cp -r "$N/litellm/tests" "$GW/litellm/"
cp "$N"/scripts/*.sh "$GW/scripts/" && chmod +x "$GW"/scripts/*.sh "$GW"/litellm/tests/*.sh
cp "$N/docker-compose.yml" "$GW/"
# код читают непривилегированные пользователи в контейнерах (dt, ocr, presidio, агенты): после переноса с Windows
# файлы бывают только для владельца (600) — тогда сервис падает с Permission denied на app.py
chmod -R a+rX,go-w "$GW/presidio-ru" "$GW/ocr-ru" "$GW/doc-translate" "$GW/agents" "$GW/librechat/skill" \
    "$GW/litellm/pii_guard.py" "$GW/litellm/tests"

step "Конфиги шлюза: дописать недостающее"
python3 - "$N/litellm/config.yaml" "$GW/litellm/config.yaml" <<'EOF'
import sys
new, cur = (open(p, encoding="utf-8").read() for p in sys.argv[1:3])
if "router_settings:" not in cur:          # повторы и запасная модель — блок целиком из нового файла
    block = new[new.index("# устойчивость к сбоям"):new.index("litellm_settings:")]
    cur = cur.replace("litellm_settings:", block + "litellm_settings:", 1)
    open(sys.argv[2], "w", encoding="utf-8").write(cur)
    print("litellm/config.yaml: добавлены повторы и запасная модель (router_settings)")
if "pii_guard.PiiRuGuard" not in cur:
    sys.exit("!! в litellm/config.yaml нет pii_guard — заменить блок guardrails вручную (см. новый config.yaml)")
EOF
grep -q 'proxy_read_timeout 600s' "$GW/nginx/ai.conf" || {
    sed -i 's/proxy_read_timeout 300s;/proxy_read_timeout 600s;\n        proxy_send_timeout 600s;/' "$GW/nginx/ai.conf"
    echo "nginx: таймаут 600 с"; }
grep -q '^OCR_API_KEY=' "$GW/.env" || { echo "OCR_API_KEY=$(openssl rand -hex 24)" >> "$GW/.env"; echo ".env: OCR_API_KEY"; }
for k in DOC_MCP_KEY DOC_INTERNAL_KEY; do     # инструмент агента «перевод файла»
    grep -q "^$k=" "$GW/.env" || { echo "$k=$(openssl rand -hex 24)" >> "$GW/.env"; echo ".env: $k"; }
done
grep -q '^LIBRECHAT_JWT_REFRESH_SECRET=' "$GW/.env" || {
    echo "LIBRECHAT_JWT_REFRESH_SECRET=$(grep '^JWT_REFRESH_SECRET=' "$LC/.env" | cut -d= -f2-)" >> "$GW/.env"
    echo ".env: LIBRECHAT_JWT_REFRESH_SECRET (вход на страницу перевода по сессии LibreChat)"; }
grep -q 'client_max_body_size 50m' "$GW/nginx/ai.conf" && {      # большие выгрузки: 50 → 200 МБ на файл
    sed -i 's/client_max_body_size 50m;/client_max_body_size 200m;/' "$GW/nginx/ai.conf"
    echo "nginx: файлы до 200 МБ"; }
grep -q 'location /translate/' "$GW/nginx/ai.conf" || {     # блок из нового ai.conf — перед «location / {»
    python3 - "$N/nginx/ai.conf" "$GW/nginx/ai.conf" <<'PYEOF'
import sys
new, cur = (open(p, encoding="utf-8").read() for p in sys.argv[1:3])
block = new[new.index("    # перевод документов (doc-translate)"):new.index("    location / {")]
open(sys.argv[2], "w", encoding="utf-8").write(cur.replace("    location / {", block + "    location / {", 1))
PYEOF
    echo "nginx: страница /translate/"; }
grep -q 'location /agent-files/' "$GW/nginx/ai.conf" || {     # стенд агентов: скачивание файлов агента
    python3 - "$N/nginx/ai.conf" "$GW/nginx/ai.conf" <<'PYEOF'
import sys
new, cur = (open(p, encoding="utf-8").read() for p in sys.argv[1:3])
block = new[new.index("    # стенд агентов: файлы"):new.index("    location / {")]
open(sys.argv[2], "w", encoding="utf-8").write(cur.replace("    location / {", block + "    location / {", 1))
PYEOF
    echo "nginx: /agent-files/ (стенд агентов)"; }
grep -q '^AGENTS_ROUTER_KEY=' "$GW/.env" || { echo "AGENTS_ROUTER_KEY=$(openssl rand -hex 24)" >> "$GW/.env"; echo ".env: AGENTS_ROUTER_KEY"; }
grep -q '^MASK_ENTITIES=.*CN_ID_CARD' "$GW/.env" || sed -i '/^MASK_ENTITIES=/s/$/,CN_ID_CARD/' "$GW/.env"

step "Сборка и запуск шлюза (presidio-ru, ocr-ru, doc-translate — тесты при сборке)"
cd "$GW"
docker compose build presidio-analyzer ocr-ru doc-translate
docker compose up -d --force-recreate presidio-analyzer ocr-ru litellm doc-translate doc-translate-mcp   # пересоздать всегда: pii_guard.py и config.yaml читаются только при старте
docker compose restart nginx 2>/dev/null || true

step "LibreChat: конфиг, скиллы, ключ распознавателя"
OCR_KEY=$(grep '^OCR_API_KEY=' "$GW/.env" | cut -d= -f2-)
grep -q '^OCR_API_KEY=' "$LC/.env" || echo "OCR_API_KEY=$OCR_KEY" >> "$LC/.env"
grep -q '^SCHEDULES_SINGLE_PROCESS=' "$LC/.env" || echo "SCHEDULES_SINGLE_PROCESS=true" >> "$LC/.env"
grep -q '^CUSTOM_FOOTER=' "$LC/.env" || echo 'CUSTOM_FOOTER="[Перевод документов (DOCX, PDF)](/translate/)"' >> "$LC/.env"
grep -q '^DOC_MCP_KEY=' "$LC/.env" || echo "DOC_MCP_KEY=$(grep '^DOC_MCP_KEY=' "$GW/.env" | cut -d= -f2-)" >> "$LC/.env"
grep -q '^AGENTS_ROUTER_KEY=' "$LC/.env" || echo "AGENTS_ROUTER_KEY=$(grep '^AGENTS_ROUTER_KEY=' "$GW/.env" | cut -d= -f2-)" >> "$LC/.env"
VERSION=$(grep -m1 '^version:' "$LC/librechat.yaml")
cp "$LC/librechat.yaml" "$LC/librechat.yaml.bak-$STAMP"
cp "$N/librechat/librechat.yaml" "$LC/librechat.yaml"
sed -i "s/^version: .*/$VERSION/" "$LC/librechat.yaml"
grep -q 'admin-panel' "$LC/docker-compose.override.yml" || cp "$N/librechat/docker-compose.override.yml" "$LC/"
mkdir -p "$LC/skill"
cp -r "$N"/librechat/skill/*/ "$LC/skill/"
cd "$LC"
docker compose up -d --force-recreate api
sleep 25
docker compose logs --since 2m api | grep -i -E "deploymentSkills|invalid|error" | tail -5 || true

if sudo test -f "$GW/agents-data/users.json"; then
    step "Стенд агентов: обновить конфиги и контейнеры (список — agents-data/users.csv)"
    sudo python3 "$GW/agents/stand.py" up
fi

step "Проверка"
cd "$GW"
docker compose ps
./scripts/smoke-test.sh
echo
echo "Готово. Копии прежних конфигов: *.bak-$STAMP. Проверить в браузере: скан в чат, агент со скиллом."
