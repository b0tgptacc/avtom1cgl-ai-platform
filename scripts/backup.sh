#!/usr/bin/env bash
# Полная копия пилота одним архивом: из него scripts/restore.sh поднимает всё на чистой ВМ с Docker.
# Внутри: дампы MongoDB (пользователи, чаты, агенты) и pgvector, весь /opt/avtom1cgl (код, конфиги, .env,
# сертификаты), файлы LibreChat без git-истории, точные версии образов (sha256) для восстановления «как было».
# cron: /etc/cron.d/avtom1cgl-backup →  30 2 * * * root /usr/local/sbin/avtom1cgl-backup.sh >> /var/log/avtom1cgl-backup.log 2>&1
# Копия только на этой ВМ от удаления ВМ не спасает — забирать архивы наружу (pull-backups.sh, см. pilot-install.md).
set -euo pipefail

DEST=${BACKUP_DEST:-/var/backups/avtom1cgl}
KEEP_DAYS=${KEEP_DAYS:-14}
READER=${BACKUP_READER:-avtbackup}     # пользователь, под которым внешний сервер забирает архивы (только чтение)
GW=/opt/avtom1cgl
LC=/opt/librechat
STAMP=$(date +%F_%H%M)
NAME="avtom1cgl-$STAMP"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "$(date -Is) начало: $NAME"
mkdir -p "$DEST" "$WORK/$NAME"
cd "$WORK/$NAME"

docker exec chat-mongodb mongodump --quiet --archive --gzip > mongodb.archive.gz
docker exec vectordb pg_dump -U myuser -Fc mydatabase > vectordb.dump      # пользователь и база — из compose LibreChat

tar -czf gateway.tar.gz -C /opt avtom1cgl
# LibreChat: только то, что не скачать заново; данные БД — в дампах выше, поисковый индекс Meilisearch пересоберётся
lc_items=()
for f in .env docker-compose.yml docker-compose.override.yml librechat.yaml certs uploads images skill; do
    [[ -e "$LC/$f" ]] && lc_items+=("$f")
done
tar -czf librechat.tar.gz -C "$LC" "${lc_items[@]}"
git -C "$LC" describe --tags --exact-match 2>/dev/null > librechat-version.txt || echo unknown > librechat-version.txt

# точные версии образов запущенных контейнеров: «имя:тег отпечаток»
docker ps --format '{{.Image}}' | sort -u | while read -r image; do
    digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$image" 2>/dev/null || true)
    if [[ -n "$digest" ]]; then echo "$image $digest"; fi     # у нашего presidio-ru отпечатка нет — собирается из кода
done > images.txt

cd "$WORK"
tar -cf "$DEST/$NAME.tar" "$NAME"
( cd "$DEST" && sha256sum "$NAME.tar" > "$NAME.tar.sha256" )
chmod 640 "$DEST/$NAME.tar" "$DEST/$NAME.tar.sha256"
if id "$READER" >/dev/null 2>&1; then
    chgrp "$READER" "$DEST" "$DEST/$NAME.tar" "$DEST/$NAME.tar.sha256"
    chmod 750 "$DEST"
else
    chmod 700 "$DEST"
fi

find "$DEST" -maxdepth 1 -name 'avtom1cgl-*.tar*' -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) готово: $DEST/$NAME.tar ($(du -h "$DEST/$NAME.tar" | cut -f1))"
