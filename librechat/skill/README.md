# Скиллы для агентов LibreChat

| Скилл | Для агента | Что умеет |
|---|---|---|
| `translate-business` | Переводчик | перевод деловых, юридических, технических текстов; термины, реквизиты, метки маскирования |
| `legal-business-writing` | Деловые тексты (юр. отдел) | письма по ГОСТ Р 7.0.97-2016, претензии и ответы, протокол разногласий, служебки, чек-лист проверки договора |
| `counterparty-check` | Деловые тексты (юр. отдел) | проверка контрагента по сервисам ФНС, арбитража, ФССП, Федресурса; признаки риска; заключение |
| `excel-merge-tables` | ИТ-помощник | объединение таблиц (Power Query, формулы, сводные), группировка, сверка списков |
| `find-files-content` | ИТ-помощник | поиск файлов по содержимому на ПК сотрудника: скрипт `Find-TextInFiles.ps1` (агенты стенда отдают его ссылкой), поиск Windows, Adobe Reader |

Формат — `SKILL.md` с заголовком `name` / `description` (как у Agent Skills), проверен по правилам LibreChat 0.8.8.

## Установка на ВМ
Папка `skill` LibreChat уже подключена к контейнеру (`./skill:/app/skill`) и входит в резервную копию.
```bash
mkdir -p /opt/librechat/skill
cp -r ~/deploy-new/deploy/librechat/skill/*/ /opt/librechat/skill/
find /opt/librechat/skill -type f -exec sed -i 's/\r$//' {} +
cd /opt/librechat && docker compose restart api
docker compose logs api | grep -i deploymentSkills     # Loaded 5 deployment skill(s)
```
Скилл с ошибкой в заголовке может помешать запуску LibreChat — после правки скилла смотреть журнал `api`.
Альтернатива — загрузка через интерфейс (файл `SKILL.md` или zip папки), тогда скилл принадлежит загрузившему.

## Как агент пользуется скиллом
1. **Сам** (модель видит каталог скиллов и загружает нужный) — нужна поддержка вызова инструментов (tool calling)
   у модели. Проверка через шлюз на ВМ:
   ```bash
   cd /opt/avtom1cgl && source .env
   docker run --rm --network ai-net curlimages/curl:8.11.1 -s http://litellm:4000/v1/chat/completions \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
     -d '{"model":"qwen-fast","messages":[{"role":"user","content":"Какая погода в Москве?"}],
          "tools":[{"type":"function","function":{"name":"get_weather","description":"Погода в городе",
          "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}'
   ```
   В ответе `"tool_calls"` с `get_weather` — работает. Ошибка про `tool choice` / `tool parser` — на удалённом vLLM
   не включён разбор вызовов инструментов (просить владельцев: `--enable-auto-tool-choice --tool-call-parser …`).
2. **По команде пользователя** — в сообщении `$translate-business` (появится подсказка после `$`). Работает всегда.
3. **Всегда** — скопировать текст `SKILL.md` (без заголовка между `---`) в поле Instructions агента. Самый надёжный
   способ для агента с одной задачей, если вызов инструментов не работает.

## Инструкции агентов (поле Instructions)
**Переводчик** (модель `qwen-fast`):
```
Ты переводчик компании. Пользуйся скиллом translate-business. Возвращай только перевод.
```
**Деловые тексты** (модель `qwen`, юр. отдел):
```
Ты помощник юридического отдела. Для писем, претензий, протоколов разногласий и проверки договоров пользуйся
скиллом legal-business-writing, для проверки контрагентов — counterparty-check. Не выдумывай факты, суммы и нормы права:
чего нет в запросе — оставляй заполнитель в квадратных скобках.
```
**ИТ-помощник** (модель `qwen`):
```
Ты помощник сотрудников по офисной рутине: Excel, поиск файлов, Windows, 1С. Для объединения и группировки таблиц
пользуйся скиллом excel-merge-tables, для поиска файлов — find-files-content. Отвечай пошагово, с названиями кнопок
как в русской версии программ. Ты не видишь компьютер пользователя — давай команды, которые он выполнит сам.
```
У каждого агента: Advanced → Max context tokens = 270000.
