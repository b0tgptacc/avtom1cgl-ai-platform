#!/usr/bin/env bash
# Забирает архивы backup.sh с ВМ на ДРУГОЙ сервер. Запускать на сервере хранения, не на ВМ:
# у ВМ нет доступа к этому серверу, поэтому удаление проекта или всей ВМ не затронет копии здесь.
#   ./pull-backups.sh avtbackup@<ip_ВМ> [/srv/backups/avtom1cgl]
# cron на сервере хранения:  15 4 * * *  /usr/local/bin/pull-backups.sh avtbackup@<ip_ВМ>
# На ВМ ключ пользователя avtbackup ограничен чтением каталога бэкапов (rrsync -ro), см. pilot-install.md.
set -euo pipefail
VM=${1:?avtbackup@<ip_ВМ>}
DEST=${2:-/srv/backups/avtom1cgl}
KEEP_DAYS=${KEEP_DAYS:-90}
mkdir -p "$DEST"
# без --delete: удалённые на ВМ архивы здесь остаются
rsync -a --ignore-existing "$VM:/" "$DEST/"
( cd "$DEST" && for f in avtom1cgl-*.tar.sha256; do sha256sum -c --quiet "$f" || echo "!! повреждён: ${f%.sha256}"; done )
find "$DEST" -maxdepth 1 -name 'avtom1cgl-*.tar*' -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) на сервере хранения: $(ls "$DEST"/avtom1cgl-*.tar 2>/dev/null | wc -l) архивов, последний: $(ls -t "$DEST"/avtom1cgl-*.tar | head -1)"
