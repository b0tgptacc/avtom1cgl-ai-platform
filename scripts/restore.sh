#!/usr/bin/env bash
# Восстановление пилота из архива backup.sh на ВМ с Docker (шаги 1–2 pilot-install.md: ОС, Docker, группа docker).
#   ./restore.sh --check avtom1cgl-2026-10-21_0230.tar   — проверить архив, ничего не меняя
#   ./restore.sh avtom1cgl-2026-10-21_0230.tar           — поднять всё из архива
# Существующие /opt/avtom1cgl и /opt/librechat не удаляются, а переименовываются в *.before-restore-<дата>.
set -euo pipefail

MODE=restore
[[ "${1:-}" == "--check" ]] && { MODE=check; shift; }
ARCHIVE=$(realpath "${1:?укажите архив avtom1cgl-*.tar}")
STAMP=$(date +%F_%H%M)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
step() { echo; echo "=== $1"; }

step "Проверка архива"
if [[ -f "$ARCHIVE.sha256" ]]; then
    ( cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "$ARCHIVE").sha256" )
else
    echo "!! нет $ARCHIVE.sha256 — целостность не проверена"
fi
tar -xf "$ARCHIVE" -C "$WORK"
B=$(echo "$WORK"/avtom1cgl-*)
for f in mongodb.archive.gz vectordb.dump gateway.tar.gz librechat.tar.gz images.txt; do
    [[ -s "$B/$f" ]] || { echo "!! в архиве нет $f"; exit 1; }
done
tar -tzf "$B/gateway.tar.gz" avtom1cgl/.env avtom1cgl/docker-compose.yml avtom1cgl/litellm/config.yaml >/dev/null
tar -tzf "$B/librechat.tar.gz" .env docker-compose.yml librechat.yaml >/dev/null
gzip -t "$B/mongodb.archive.gz"
echo "состав в порядке; LibreChat $(cat "$B/librechat-version.txt"); образов: $(wc -l < "$B/images.txt")"
[[ "$MODE" == check ]] && { echo "проверка пройдена, ничего не изменено"; exit 0; }

step "Каталоги"
for d in /opt/avtom1cgl /opt/librechat; do
    if [[ -e "$d" ]]; then
        sudo mv "$d" "$d.before-restore-$STAMP"
        echo "$d → $d.before-restore-$STAMP"
    fi
done
sudo tar -xzf "$B/gateway.tar.gz" -C /opt
sudo mkdir -p /opt/librechat
sudo tar -xzf "$B/librechat.tar.gz" -C /opt/librechat
sudo mkdir -p /opt/librechat/{data-node,uploads,images,logs}
sudo chown -R "$(id -u):$(id -g)" /opt/avtom1cgl /opt/librechat
chmod 600 /opt/avtom1cgl/.env /opt/librechat/.env

step "Сеть и образы тех же версий, что были в работе"
docker network inspect ai-net >/dev/null 2>&1 || docker network create ai-net
while read -r image digest; do
    [[ "$image" == avtom1cgl/* ]] && continue          # наш образ собирается из исходников ниже
    docker pull -q "$digest" && docker tag "$digest" "$image"
done < "$B/images.txt"

step "Шлюз маскирования"
cd /opt/avtom1cgl
docker compose build presidio-analyzer ocr-ru doc-translate
docker compose up -d presidio-analyzer ocr-ru litellm doc-translate doc-translate-mcp

step "Базы LibreChat"
cd /opt/librechat
docker compose up -d mongodb vectordb
for i in $(seq 60); do
    docker exec chat-mongodb mongosh --quiet --eval 'db.runCommand({ping: 1}).ok' >/dev/null 2>&1 \
        && docker exec vectordb pg_isready -U myuser -d mydatabase >/dev/null 2>&1 && break
    [[ $i == 60 ]] && { echo "!! базы не запустились"; exit 1; }
    sleep 2
done
docker exec -i chat-mongodb mongorestore --quiet --archive --gzip --drop < "$B/mongodb.archive.gz"
docker exec -i vectordb pg_restore -U myuser -d mydatabase --clean --if-exists < "$B/vectordb.dump" \
    || echo "!! pg_restore с предупреждениями (обычно безвредно при пустой базе)"

step "LibreChat и HTTPS"
docker compose up -d
cd /opt/avtom1cgl
docker compose up -d nginx

step "Проверка"
./scripts/smoke-test.sh
echo
echo "Восстановлено. Открыть сайт, войти, проверить чаты и агентов."
echo "Старые каталоги (если были): /opt/*.before-restore-$STAMP — удалить после проверки."
