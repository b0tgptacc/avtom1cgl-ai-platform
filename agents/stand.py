#!/usr/bin/env python3
"""Стенд агентов Hermes и OpenClaw для сравнения на одних задачах. Только стандартная библиотека Python.

У каждого тестировщика по контейнеру каждого движка: своя память, свои сессии и свои навыки,
доступ на чтение к своим вложениям из LibreChat. Навыки отдела и общие — одна папка на оба движка,
в агентах только для чтения. Навык, который агент создал сам, сначала личный; в отдел его переносит
администратор после проверки (promote).

  sudo python3 stand.py up                       создать или обновить стенд по users.csv и запустить
  sudo python3 stand.py status                   кто есть, контейнеры, память
  sudo python3 stand.py skills <email>           навыки, которые агенты сотрудника создали сами
  sudo python3 stand.py promote <email> <движок> <навык> [отдел]   навык сотрудника → навыки отдела
                                                 (отдел company — общие навыки для всех сотрудников)
  sudo python3 stand.py ratings                  время ответов и оценки 👍/👎 по движкам, замечания
  sudo python3 stand.py down                     остановить стенд (данные остаются)

Данные: /opt/avtom1cgl/agents-data (users.csv — список, users.json — ключи, users/ — память, skills/ — навыки).
users.csv: строки «email;отдел;движки», движки — hermes,openclaw (пусто — оба). Отдел — латиницей: legal, it, finance."""
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GW = Path(os.getenv("AVTOM1CGL_DIR", "/opt/avtom1cgl"))
LC = Path(os.getenv("LIBRECHAT_DIR", "/opt/librechat"))
DATA = Path(os.getenv("AGENTS_DATA", str(GW / "agents-data")))
SKILLS_SRC = HERE.parent / "librechat" / "skill"       # общие навыки — те же, что в LibreChat
MONGO = os.getenv("LIBRECHAT_MONGO", "chat-mongodb")

# образы закреплены: обновление движка — осознанная смена строки (hermes.digest) и повторная проверка на стенде.
# Поверх — одинаковые для обоих инструменты для документов (images/): DOCX, XLSX, PDF, LibreOffice, шрифты
HERMES_BASE = "nousresearch/hermes-agent@sha256:{digest}"
OPENCLAW_BASE = "ghcr.io/openclaw/openclaw:2026.10.5"
HERMES_IMAGE = "avtom1cgl/agent-hermes:{tag}"
OPENCLAW_IMAGE = "avtom1cgl/agent-openclaw:2026.10.5-{tools}"
ROUTER_IMAGE = "avtom1cgl/agents-router:latest"
AGENT_MODEL = os.getenv("AGENT_MODEL", "qwen")          # модель шлюза для агентов: qwen (с рассуждениями) или qwen-fast
CONTEXT = 262144
ENGINES = ("hermes", "openclaw")
HERMES_UID, OPENCLAW_UID, ROUTER_UID = 10000, 1000, 10004

INSTRUCTIONS = """# Правила работы (стенд агентов {company})

Ты — рабочий помощник сотрудника компании. Отвечай по-русски, по делу.

## Файлы
- Файлы, которые сотрудник прикрепил в чате, лежат в папке `{uploads}` (только чтение).
  Имя файла там: `<код>__<имя файла>`. Ищи по имени без учёта регистра и пробелов.
  Текст вложения в сообщении — только начало большого файла (до 100 тыс. токенов): работай с самим файлом
  из `{uploads}`. Большой XML, CSV, Excel разбирай программой (выбери нужные поля, посчитай, сведи в таблицу),
  модели показывай только результат, а не весь файл. XML больше 50 МБ читай потоково (`xml.etree.ElementTree.iterparse`
  с очисткой разобранных элементов), иначе не хватит памяти. Архив ZIP распакуй в `work` и работай с файлами из него.
- Готовые файлы для сотрудника (перевод, таблица, письмо) клади в папку `{outbox}` и в ответе давай
  ссылку вида `[имя файла](/agent-files/{engine}/<имя файла>)` — по ней сотрудник скачает файл из чата.
  Не вставляй содержимое большого файла в ответ и не предлагай «сохранить страницу» — давай файл.
{translate}

## Данные
- Метки вида <PERSON_1>, <PHONE_NUMBER_2> — замаскированные персональные данные. Переноси их как есть,
  в итоговом ответе они автоматически заменятся на настоящие значения.
- Интернета нет. Не пытайся устанавливать пакеты из сети и не обращайся к внешним сайтам.

## Профиль задачи
- Перед каждым запросом распределитель добавляет системный `TASK PROFILE`. Он задаёт допустимый метод работы
  для перевода, извлечения фактов, обезличивания, расчёта, создания документа или общего запроса.
- Следуй этому профилю; не заменяй проверяемый инструмент свободным рассуждением. Если данных, файла или
  подходящего инструмента нет, сообщи короткий следующий шаг вместо ошибки, выдумывания фактов или установки ПО.

## Навыки
- Перед задачей посмотри список навыков: навыки отдела и общие навыки уже описывают, как решать типовые задачи.
- Решил новую повторяющуюся задачу (несколько шагов, пригодится коллегам) — сохрани навык:
  короткое название латиницей через дефис, в описании — когда применять. Навыки отдела менять нельзя.

## Память
- Запоминай устойчивые факты о сотруднике и его работе (должность, частые контрагенты, предпочтения в
  оформлении). Не запоминай персональные данные третьих лиц и содержимое документов.
"""


def run(cmd, check=True, capture=True):
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)


def env_file(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def ensure_env_key(path: Path, key: str, value: str) -> None:
    if key not in env_file(path):
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n{key}={value}\n")
        print(f"{path}: добавлен {key}")


def slug_for(email: str) -> str:
    local = re.sub(r"[^a-z0-9]+", "-", email.split("@")[0].lower()).strip("-")[:20] or "user"
    return f"{local}-{hashlib.sha256(email.lower().encode()).hexdigest()[:4]}"


def librechat_id(email: str) -> str:
    """id сотрудника в LibreChat (по нему лежат его вложения). Пусто — ещё не входил в чат."""
    js = ("const u = db.users.findOne({email: %s}, {_id: 1}); print(u ? u._id.toString() : '')"
          % json.dumps(email.lower()))
    r = run(["docker", "exec", MONGO, "mongosh", "LibreChat", "--quiet", "--eval", js], check=False)
    value = (r.stdout or "").strip().splitlines()[-1:] or [""]
    return value[0] if re.fullmatch(r"[0-9a-f]{24}", value[0]) else ""


def read_users() -> list:
    path = DATA / "users.csv"
    if not path.exists():
        path.write_text("# email;отдел;движки (hermes,openclaw — пусто: оба)\n# ivanov@corp.local;legal;\n", encoding="utf-8")
        sys.exit(f"Заполните {path} и запустите снова")
    rows = []
    for row in csv.reader((l for l in path.read_text(encoding="utf-8-sig").splitlines()
                           if l.strip() and not l.lstrip().startswith("#")), delimiter=";"):
        row = [c.strip() for c in row] + ["", ""]
        email, dept, engines = row[0].lower(), (row[1] or "common").lower(), row[2]
        if not re.fullmatch(r"[^@\s]+@[^@\s]+", email) or not re.fullmatch(r"[a-z0-9-]{2,30}", dept) \
                or dept in ("company", "tools"):
            sys.exit(f"users.csv: неверная строка {row[:3]} (отдел — латиницей: legal, it, finance)")
        chosen = [e.strip() for e in engines.split(",") if e.strip()] or list(ENGINES)
        if set(chosen) - set(ENGINES):
            sys.exit(f"users.csv: движки — только {', '.join(ENGINES)}: {row[:3]}")
        rows.append((email, dept, chosen))
    return rows

# перевод документов: Hermes — готовой командой целиком; OpenClaw переводит сам (с переводом команды выходило хуже,
# решение 22.10), а команда только раскладывает его перевод по местам оригинала (--extract / --apply, 26.10)
TRANSLATE = {
    "hermes": "- Перевод файла — только командой `translate-document` (навык document-tools): она сохраняет вёрстку"
              " DOCX, PDF\n  и сканов. Свой код перевода документа не пиши. Мы за качество, а не за скорость:"
              " дождись результата и проверь его.",
    "openclaw": """- Перевод документа (DOCX, PDF, скан) переводишь сам, а вёрстку оригинала сохраняет команда
  `translate-document` — свой код сборки документа не пиши, документ не пересоздавай:
  1. `translate-document uploads/<файл> --to ru --extract --out work` → `work/<имя>.segments.json`
     (абзацы документа в поле `text`, скан распознаётся сам).
  2. Переведи каждый абзац и впиши в поле `result`: полностью, без пересказа, термины одинаково во всём
     документе. Маркеры ⟦n⟧…⟦/n⟧ (жирный, курсив) сохрани. Номера пунктов, числа, единицы и формулы
     не меняй. Свои пометки в текст не добавляй. Большой файл переводи частями по 30–50 абзацев
     (читай и записывай JSON скриптом, а не руками).
  3. `translate-document work/<имя>.segments.json --apply --out outbox` → PDF (главный результат,
     страницы как в оригинале) и DOCX для правки.
  Замечания переводчика (неразборчивые места, спорные термины) — в ответе в чате, не в документе.""",
}


def hermes_config(agent_key: str) -> str:
    return f"""# создаётся stand.py — правки вносить в stand.py, иначе перезапишутся
model:
  default: {AGENT_MODEL}
  provider: custom
  base_url: http://agents-router:8080/llm/v1
  api_key: {agent_key}
  context_length: {CONTEXT}
# Typed, per-agent MCP bridge; the key is scoped to this agent and never reaches quality-tools.
mcp_servers:
  quality_tools:
    url: http://agents-router:8080/quality/mcp
    headers:
      Authorization: "Bearer {agent_key}"
    timeout: 120
    connect_timeout: 30
terminal:
  backend: local
  cwd: /opt/data/workspace
  timeout: 3600              # перевод большого документа (translate-document) идёт десятки минут
skills:
  external_dirs:
    - /skills/dept
    - /skills/company        # общие навыки, доработанные на ВМ (заменяют одноимённые из common)
    - /skills/common
    - /skills/tools          # document-tools: перевод командой translate-document — только у Hermes
# без интернета, браузера и картинок: их содержимое не проходит маскирование
platform_toolsets:
  api_server: [terminal, file, code_execution, skills, todo]
# подтверждать команды в LibreChat некому — без этого Hermes ждёт ответа 5 минут и отменяет команду.
# Допустимо: агент заперт в своём контейнере без сети и чужих данных; жёсткий запрет опасных команд остаётся
approvals:
  mode: "off"
gateway:
  platforms:
    api_server:
      tool_progress_events: false
updates:
  check: false
"""


def openclaw_config(agent_key: str, token: str) -> str:
    return json.dumps({
        "gateway": {
            "mode": "local", "port": 18789, "bind": "lan",
            "auth": {"mode": "token", "token": token},
            "http": {"endpoints": {"chatCompletions": {"enabled": True}}},
        },
        "models": {"providers": {"gw": {
            "baseUrl": "http://agents-router:8080/llm/v1", "apiKey": agent_key,
            "api": "openai-completions", "timeoutSeconds": 900,
            "models": [{"id": AGENT_MODEL, "name": "Qwen", "reasoning": AGENT_MODEL == "qwen", "input": ["text"],
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        "contextWindow": CONTEXT, "maxTokens": 32768}],
        }}},
        "agents": {"defaults": {"model": f"gw/{AGENT_MODEL}", "workspace": "/home/node/.openclaw/workspace"}},
        "mcp": {"servers": {"quality-tools": {
            "url": "http://agents-router:8080/quality/mcp", "transport": "streamable-http",
            "headers": {"Authorization": f"Bearer {agent_key}"}, "timeout": 120, "connectTimeout": 30,
        }}},
        "skills": {"load": {"extraDirs": ["/skills/dept", "/skills/company", "/skills/common"]}},
        "tools": {
            "profile": "coding",  # fs + runtime only; no browser/search/messaging surface in corporate document profile
            "exec": {"timeoutSeconds": 3600},
        },
    }, ensure_ascii=False, indent=2)


def write(path: Path, text: str, uid: int = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if uid is not None and os.geteuid() == 0:
        os.chown(path, uid, uid)


def prepare_user(email: str, u: dict) -> None:
    home = DATA / "users" / u["slug"]
    # адрес модели и ключ агента для translate-document у Hermes (в контейнере — /etc/agent/llm.env, только чтение)
    write(home / "llm.env", f"LLM_BASE=http://agents-router:8080/llm/v1\nLITELLM_MASTER_KEY={u['agent_key']}\n"
                            f"LLM_MODEL={AGENT_MODEL}\n")
    os.chmod(home / "llm.env", 0o644)
    for engine in u["engines"]:
        files = home / f"files-{engine}"
        files.mkdir(parents=True, exist_ok=True)
        os.chmod(files, 0o1777)          # агент пишет, распределитель отдаёт сотруднику
    if "hermes" in u["engines"]:
        h = home / "hermes"
        (h / "workspace").mkdir(parents=True, exist_ok=True)
        write(h / "config.yaml", hermes_config(u["agent_key"]))
        # без встроенного каталога (~60 навыков: Apple, соцсети, GitHub…) — им нужен интернет, а в подсказке
        # модели они занимают место; навыки — наши общие и отдела
        if not (h / "skills" / ".bundled_manifest").exists():
            write(h / ".no-bundled-skills", "")
        write(h / "SOUL.md", INSTRUCTIONS.format(company="Avtom1cGL", uploads="/opt/data/uploads",
                                                 outbox="/opt/data/outbox", engine="hermes",
                                                 translate=TRANSLATE["hermes"]))
        # ключ API — в .env профиля (сам Hermes читает его при старте)
        env = h / ".env"
        lines = [l for l in (env.read_text(encoding="utf-8").splitlines() if env.exists() else [])
                 if not l.startswith(("API_SERVER_", "HERMES_"))]
        write(env, "\n".join(lines + ["API_SERVER_ENABLED=true", "API_SERVER_HOST=0.0.0.0",
                                      f"API_SERVER_KEY={u['engines']['hermes']['key']}"]) + "\n")
    if "openclaw" in u["engines"]:
        o = home / "openclaw"
        (o / "workspace").mkdir(parents=True, exist_ok=True)
        write(o / "openclaw.json", openclaw_config(u["agent_key"], u["engines"]["openclaw"]["key"]), OPENCLAW_UID)
        write(o / "workspace" / "AGENTS.md", INSTRUCTIONS.format(
            company="Avtom1cGL", uploads="uploads", outbox="outbox", engine="openclaw",
            translate=TRANSLATE["openclaw"]), OPENCLAW_UID)
        if os.geteuid() == 0:
            for root, dirs, fls in os.walk(o):
                for name in dirs + fls:
                    os.lchown(os.path.join(root, name), OPENCLAW_UID, OPENCLAW_UID)
            os.chown(o, OPENCLAW_UID, OPENCLAW_UID)


def sync_skills(depts: set) -> None:
    # common — навыки LibreChat (оба движка); tools — навыки агентов: document-tools (translate-document), только Hermes;
    # company — общие навыки, которые дорабатывает администратор на ВМ: обновление их не трогает, и навык company
    # заменяет одноимённый из common
    company = DATA / "skills" / "company"
    company.mkdir(parents=True, exist_ok=True)
    for root, dst in ((SKILLS_SRC, DATA / "skills" / "common"), (HERE / "skills", DATA / "skills" / "tools")):
        dst.mkdir(parents=True, exist_ok=True)
        for src in (root.iterdir() if root.is_dir() else []):
            if dst.name == "common" and (company / src.name / "SKILL.md").exists():
                shutil.rmtree(dst / src.name, ignore_errors=True)
                continue
            if (src / "SKILL.md").exists():
                shutil.rmtree(dst / src.name, ignore_errors=True)
                shutil.copytree(src, dst / src.name)
                run(["chmod", "-R", "a+rX,go-w", str(dst / src.name)])     # агенты читают не от root
                if dst.name == "tools":                 # до 22.10 лежал в common и был виден OpenClaw
                    shutil.rmtree(DATA / "skills" / "common" / src.name, ignore_errors=True)
    for d in depts:
        (DATA / "skills" / d).mkdir(parents=True, exist_ok=True)


def compose(users: dict, env: dict, images: tuple) -> str:
    hermes_image, openclaw_image = images
    log = "    logging: {driver: json-file, options: {max-size: 20m, max-file: '3'}}\n"
    out = ["# создаётся stand.py — не править вручную", "services:",
           "  agents-router:",
           f"    image: {ROUTER_IMAGE}",
           "    container_name: agents-router",
           "    restart: unless-stopped",
           "    environment:",
           f"      ROUTER_KEY: \"{env['AGENTS_ROUTER_KEY']}\"",
           f"      LITELLM_MASTER_KEY: \"{env['LITELLM_MASTER_KEY']}\"",
           f"      JWT_REFRESH_SECRET: \"{env['LIBRECHAT_JWT_REFRESH_SECRET']}\"",
           "      LLM_BASE: http://litellm:4000/v1",
           "    volumes:",
           f"      - {DATA}/users.json:/data/users.json:ro",
           f"      - {DATA}/users:/data/users:ro",
           f"      - {DATA}/stats:/data/stats",
           "    ports: ['127.0.0.1:8090:8080']    # только для compare.py на самой ВМ",
           "    networks: [ai-net, agents-net]",
           log.rstrip("\n")]
    for email, u in sorted(users.items()):
        home = DATA / "users" / u["slug"]
        uploads = LC / "uploads" / u["id"] if u.get("id") else None
        skills = [f"      - {DATA}/skills/{u['dept']}:/skills/dept:ro", f"      - {DATA}/skills/company:/skills/company:ro",
                  f"      - {DATA}/skills/common:/skills/common:ro"]
        common = ["    restart: unless-stopped", "    networks: [agents-net]",
                  "    security_opt: [no-new-privileges:true]", log.rstrip("\n")]
        if "hermes" in u["engines"]:
            out += [f"  agent-hermes-{u['slug']}:  # {email}",
                    f"    image: {hermes_image}",
                    f"    container_name: agent-hermes-{u['slug']}",
                    "    command: [gateway, run]",
                    "    mem_limit: 1g",
                    "    environment:",
                    f"      HERMES_UID: '{HERMES_UID}'",
                    f"      HERMES_GID: '{HERMES_UID}'",
                    "      QUALITY_TOOLS_BASE: \"http://agents-router:8080/quality/v1\"",
                    f"      QUALITY_TOOLS_API_KEY: \"{u['agent_key']}\"",
                    "    volumes:",
                    f"      - {home}/hermes:/opt/data",
                    f"      - {home}/files-hermes:/opt/data/outbox",
                    f"      - {DATA}/skills/tools:/skills/tools:ro",
                    f"      - {home}/llm.env:/etc/agent/llm.env:ro"] + \
                   ([f"      - {uploads}:/opt/data/uploads:ro"] if uploads else []) + skills + common
        if "openclaw" in u["engines"]:
            out += [f"  agent-openclaw-{u['slug']}:  # {email}",
                    f"    image: {openclaw_image}",
                    f"    container_name: agent-openclaw-{u['slug']}",
                    "    command: [node, dist/index.js, gateway, --bind, lan, --port, '18789']",
                    "    init: true",
                    "    mem_limit: 2g",
                    "    cap_drop: [NET_RAW, NET_ADMIN]",
                    "    environment:",
                    "      HOME: /home/node",
                    "      NODE_OPTIONS: --max-old-space-size=1024",
                    "      OPENCLAW_DISABLE_BONJOUR: '1'",
                    f"      OPENCLAW_GATEWAY_TOKEN: \"{u['engines']['openclaw']['key']}\"",
                    "      QUALITY_TOOLS_BASE: \"http://agents-router:8080/quality/v1\"",
                    f"      QUALITY_TOOLS_API_KEY: \"{u['agent_key']}\"",
                    "    volumes:",
                    f"      - {home}/openclaw:/home/node/.openclaw",
                    f"      - {home}/files-openclaw:/home/node/.openclaw/workspace/outbox"] + \
                   ([f"      - {uploads}:/home/node/.openclaw/workspace/uploads:ro"] if uploads else []) + skills + common
    out += ["networks:",
            "  ai-net: {external: true}",
            "  agents-net:",
            "    name: avtom1cgl-agents",
            "    internal: true      # у агентов нет интернета: только распределитель (и через него шлюз маскирования)"]
    return "\n".join(out) + "\n"


def build_images() -> str:
    """Образы агентов и распределителя (тесты распределителя — при сборке). Возвращает имя образа Hermes."""
    digest = (HERE / "hermes.digest").read_text(encoding="utf-8").strip()
    # метка образа — от содержимого images/: поменяли набор инструментов — образы пересоберутся
    sources = sorted(p for p in (HERE / "images").iterdir() if p.is_file()) + \
        [HERE.parent / "doc-translate" / n for n in ("docx_translate.py", "pdf_translate.py", "llm.py")]
    tools = hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest()[:8]
    hermes = HERMES_IMAGE.format(tag=f"{digest[:12]}-{tools}")
    openclaw = OPENCLAW_IMAGE.format(tools=tools)
    for image, base, dockerfile in ((hermes, HERMES_BASE.format(digest=digest), "Dockerfile.hermes"),
                                    (openclaw, OPENCLAW_BASE, "Dockerfile.openclaw")):
        if run(["docker", "image", "inspect", image], check=False).returncode != 0:
            print(f"Сборка {image} (несколько минут, нужен доступ к Docker Hub/ghcr.io и репозиториям Debian/PyPI)…")
            run(["docker", "build", "-q", "--build-arg", f"BASE={base}", "-f", str(HERE / "images" / dockerfile),
                 "-t", image, str(HERE.parent)], capture=False)
    print("Сборка распределителя (тесты при сборке)…")
    run(["docker", "build", "-q", "-t", ROUTER_IMAGE, str(HERE / "router")], capture=False)
    return hermes, openclaw


def dc(*args, capture=False):
    return run(["docker", "compose", "-p", "avtom1cgl-agents", "-f", str(DATA / "docker-compose.yml"), *args],
               capture=capture)


def cmd_up() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o750)                    # ключи и память сотрудников — только root (контейнерам — через монтирование)
    gw_env = GW / ".env"
    ensure_env_key(gw_env, "AGENTS_ROUTER_KEY", secrets.token_hex(24))
    ensure_env_key(LC / ".env", "AGENTS_ROUTER_KEY", env_file(gw_env)["AGENTS_ROUTER_KEY"])
    env = env_file(gw_env)
    for k in ("LITELLM_MASTER_KEY", "LIBRECHAT_JWT_REFRESH_SECRET"):
        if not env.get(k):
            sys.exit(f"нет {k} в {gw_env} — сначала apply-update.sh")

    path = DATA / "users.json"
    known = json.loads(path.read_text(encoding="utf-8"))["users"] if path.exists() else {}
    users = {}
    for email, dept, engines in read_users():
        u = known.get(email, {})
        u.setdefault("slug", slug_for(email))
        u.setdefault("agent_key", "ak-" + secrets.token_hex(24))
        u["dept"] = dept
        u["id"] = u.get("id") or librechat_id(email)
        if not u["id"]:
            print(f"!! {email}: ещё не входил в чат — вложения и ссылки на файлы заработают после входа и повторного up")
        u["engines"] = {e: (u.get("engines") or {}).get(e) or {"key": secrets.token_hex(24)} for e in engines}
        users[email] = u
        if u.get("id"):
            (LC / "uploads" / u["id"]).mkdir(parents=True, exist_ok=True)
        prepare_user(email, u)
    write(path, json.dumps({"users": users}, ensure_ascii=False, indent=2))
    os.chmod(path, 0o644)                    # распределитель читает от своего пользователя; каталог DATA — 0750
    sync_skills({u["dept"] for u in users.values()})
    (DATA / "stats").mkdir(exist_ok=True)                 # время ответов и оценки (пишет распределитель)
    if os.geteuid() == 0:
        os.chown(DATA / "stats", ROUTER_UID, ROUTER_UID)
    images = build_images()
    write(DATA / "docker-compose.yml", compose(users, env, images))
    os.chmod(DATA / "docker-compose.yml", 0o600)
    dc("up", "-d", "--remove-orphans")
    print(f"\nСтенд: {len(users)} сотр. Проверка: sudo python3 {__file__} status")


def cmd_status() -> None:
    users = json.loads((DATA / "users.json").read_text(encoding="utf-8"))["users"]
    ps = run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"]).stdout
    state = dict(l.split("\t", 1) for l in ps.splitlines() if "\t" in l)
    stats = run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"], check=False).stdout
    mem = dict(l.split("\t", 1) for l in stats.splitlines() if "\t" in l)
    print(f"{'сотрудник':32} {'отдел':10} {'движок':9} {'состояние':24} память")
    print(f"{'(распределитель)':32} {'':10} {'':9} {state.get('agents-router', 'нет')[:24]:24} {mem.get('agents-router', '')}")
    for email, u in sorted(users.items()):
        for e in u["engines"]:
            name = f"agent-{e}-{u['slug']}"
            print(f"{email:32} {u['dept']:10} {e:9} {state.get(name, 'нет')[:24]:24} {mem.get(name, '')}"
                  + ("" if u.get("id") else "  (не входил в чат)"))


def personal_skills(u: dict) -> dict:
    """Навыки, созданные агентами сотрудника: {движок: {имя: папка}}."""
    home = DATA / "users" / u["slug"]
    roots = {"hermes": [home / "hermes" / "skills"],
             "openclaw": [home / "openclaw" / "workspace" / "skills", home / "openclaw" / "skills"]
                         + list((home / "openclaw" / "agents").glob("*/agent/workshop-skills"))}
    manifest = home / "hermes" / "skills" / ".bundled_manifest"      # встроенные навыки Hermes — не в счёт
    bundled = {l.split(":", 1)[0] for l in manifest.read_text(encoding="utf-8").splitlines()} if manifest.exists() else set()
    found = {}
    for engine, dirs in roots.items():
        for d in dirs:
            for skill in d.rglob("SKILL.md") if d.is_dir() else []:
                if not (engine == "hermes" and skill.parent.name in bundled):
                    found.setdefault(engine, {})[skill.parent.name] = skill.parent
    return found


def cmd_skills(email: str) -> None:
    users = json.loads((DATA / "users.json").read_text(encoding="utf-8"))["users"]
    u = users.get(email.lower()) or sys.exit(f"{email}: нет в стенде")
    for engine, skills in personal_skills(u).items():
        for name, path in sorted(skills.items()):
            print(f"{engine:9} {name:40} {path}")


def cmd_promote(email: str, engine: str, name: str, dept: str = None) -> None:
    users = json.loads((DATA / "users.json").read_text(encoding="utf-8"))["users"]
    u = users.get(email.lower()) or sys.exit(f"{email}: нет в стенде")
    src = personal_skills(u).get(engine, {}).get(name) or sys.exit(f"нет навыка {name} у {email} в {engine}")
    if dept in ("common", "tools"):
        sys.exit("common и tools обновляются из репозитория — для общих навыков: company")
    dst = DATA / "skills" / (dept or u["dept"]) / name
    if dst.exists():
        shutil.move(str(dst), str(dst) + ".prev")
    shutil.copytree(src, dst)
    run(["chmod", "-R", "a+rX,go-w", str(dst)])
    if dept == "company":
        shutil.rmtree(DATA / "skills" / "common" / name, ignore_errors=True)    # заменяет одноимённый общий
    where = "общие навыки (все сотрудники)" if dept == "company" else f"навыки отдела {dept or u['dept']}"
    print(f"{name} → {where}: {dst}. Агенты увидят его в новом чате.")


def _jsonl(name: str) -> list:
    path = DATA / "stats" / f"{name}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def cmd_ratings() -> None:
    """Время ответов и оценки по движкам; оценка ответа — последняя поставленная."""
    runs, rated = _jsonl("runs"), {}
    for r in _jsonl("ratings"):
        rated[r["run"]] = {**rated.get(r["run"], {}), **{k: v for k, v in r.items() if v not in ("", None)}}
    print(f"{'движок':9} {'ответов':>8} {'ошибок':>7} {'медиана':>9} {'среднее':>9} {'👍':>4} {'👎':>4}")
    for e in ENGINES:
        rs = [r for r in runs if r["engine"] == e]
        secs = sorted(r["seconds"] for r in rs if r["status"] == "ok")
        ups = sum(1 for r in rated.values() if r["engine"] == e and r["score"] == "up")
        downs = sum(1 for r in rated.values() if r["engine"] == e and r["score"] == "down")
        med = secs[len(secs) // 2] if secs else 0
        avg = sum(secs) / len(secs) if secs else 0
        print(f"{e:9} {len(rs):>8} {len(rs) - len(secs):>7} {med:>8.0f}с {avg:>8.0f}с {ups:>4} {downs:>4}")
    notes = sorted((r for r in rated.values() if r.get("comment")), key=lambda r: r["ts"])
    if notes:
        print("\nЗамечания:")
        for r in notes:
            when = __import__("datetime").datetime.fromtimestamp(r["ts"]).strftime("%d.%m %H:%M")
            print(f"  {when} {r['engine']:9} {'👍' if r['score'] == 'up' else '👎'} {r['slug']}: {r['comment']}")


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["ratings"]:
        return cmd_ratings()
    if not args or args[0] not in ("up", "status", "skills", "promote", "down"):
        sys.exit(__doc__)
    if args[0] == "up":
        cmd_up()
    elif args[0] == "status":
        cmd_status()
    elif args[0] == "skills" and len(args) == 2:
        cmd_skills(args[1])
    elif args[0] == "promote" and len(args) in (4, 5):
        cmd_promote(*args[1:])
    elif args[0] == "down":
        dc("down")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
