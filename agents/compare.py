#!/usr/bin/env python3
"""Сравнение Hermes и OpenClaw на одних задачах: каждую задачу оба агента одного сотрудника решают одновременно,
результаты — в один отчёт HTML (ответ, время, файлы, что проверить). Только стандартная библиотека Python.

  sudo python3 compare.py <email тестировщика> [id задачи ...]      все задачи или выбранные
  sudo python3 compare.py --list                                     список задач

Задачи — tasks/tasks.json (и agents-data/tasks.json, если есть: свои задачи компании). Файлы задач кладутся
во вложения сотрудника в LibreChat, как будто он прикрепил их в чате. Запросы идут через распределитель
(127.0.0.1:8090), то есть через те же контейнеры и шлюз маскирования, что и из чата.
Отчёт: agents-data/reports/<дата_время>/report.html, файлы агентов — рядом. Оценки ставит человек."""
import html
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
GW = Path(os.getenv("AVTOM1CGL_DIR", "/opt/avtom1cgl"))
LC = Path(os.getenv("LIBRECHAT_DIR", "/opt/librechat"))
DATA = Path(os.getenv("AGENTS_DATA", str(GW / "agents-data")))
ROUTER = os.getenv("AGENTS_ROUTER_URL", "http://127.0.0.1:8090")
TIMEOUT = int(os.getenv("TASK_TIMEOUT", "1800"))
ENGINES = ("hermes", "openclaw")
TITLES = {"hermes": "Hermes", "openclaw": "OpenClaw"}


def env_value(key: str) -> str:
    for line in (GW / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit(f"нет {key} в {GW / '.env'} — сначала stand.py up")


def load_tasks() -> list:
    tasks = []
    for path in (HERE / "tasks" / "tasks.json", DATA / "tasks.json"):
        if path.exists():
            tasks += json.loads(path.read_text(encoding="utf-8"))["tasks"]
    return tasks


def task_file(name: str) -> Path:
    for d in (DATA / "task-files", HERE / "tasks" / "files"):
        if (d / name).exists():
            return d / name
    sys.exit(f"нет файла задачи {name} (tasks/files или agents-data/task-files)")


def ask(engine: str, email: str, conv: str, prompt: str, key: str) -> dict:
    body = json.dumps({"model": engine, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{ROUTER}/{engine}/v1/chat/completions", data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "X-User-Email": email, "X-Conversation-Id": conv})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
        msg = data["choices"][0]["message"]
        return {"ok": True, "text": msg.get("content") or "", "seconds": time.time() - started,
                "tokens": (data.get("usage") or {}).get("total_tokens")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "text": f"HTTP {e.code}: {e.read().decode(errors='replace')[:2000]}",
                "seconds": time.time() - started}
    except Exception as e:                                 # таймаут, обрыв — тоже результат сравнения
        return {"ok": False, "text": f"{type(e).__name__}: {e}", "seconds": time.time() - started}


def run_task(task: dict, email: str, user: dict, key: str, outdir: Path) -> dict:
    uploads = LC / "uploads" / user["id"] if user.get("id") else None
    for name in task.get("files", []):
        if not uploads:
            return {e: {"ok": False, "text": "сотрудник ещё не входил в чат — вложения недоступны", "seconds": 0}
                    for e in ENGINES}
        uploads.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task_file(name), uploads / f"{uuid.uuid4().hex[:24]}__{name}")

    result = {}

    def one(engine: str):
        files_dir = DATA / "users" / user["slug"] / f"files-{engine}"
        before = {p.name: p.stat().st_mtime for p in files_dir.iterdir()} if files_dir.is_dir() else {}
        conv, steps = f"cmp-{task['id']}-{uuid.uuid4().hex[:8]}", []
        for step in task["steps"]:
            step = step if isinstance(step, dict) else {"prompt": step}
            if step.get("new_chat"):
                conv = f"cmp-{task['id']}-{uuid.uuid4().hex[:8]}"
            steps.append({"prompt": step["prompt"], **ask(engine, email, conv, step["prompt"], key)})
        made = []
        if files_dir.is_dir():
            for p in sorted(files_dir.iterdir()):
                if p.is_file() and before.get(p.name) != p.stat().st_mtime:
                    dst = outdir / engine / task["id"] / p.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dst)
                    made.append(str(dst.relative_to(outdir)))
        result[engine] = {"steps": steps, "files": made, "seconds": sum(s["seconds"] for s in steps),
                          "ok": all(s["ok"] for s in steps)}

    threads = [threading.Thread(target=one, args=(e,)) for e in ENGINES if e in user["engines"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return result


def report(results: list, email: str, outdir: Path) -> Path:
    def cell(r):
        if not r:
            return "<td class=na>не подключён</td>"
        parts = []
        for i, s in enumerate(r["steps"], 1):
            head = f"<div class=q>{i}. {html.escape(s['prompt'][:300])}</div>" if len(r["steps"]) > 1 else ""
            parts.append(f"{head}<div class='a {'' if s['ok'] else 'err'}'>{html.escape(s['text'])}</div>")
        files = "".join(f"<li><a href='{html.escape(f)}'>{html.escape(Path(f).name)}</a></li>" for f in r["files"])
        return (f"<td><div class=meta>{r['seconds']:.0f} с · {'ок' if r['ok'] else 'ошибка'}</div>{''.join(parts)}"
                f"{f'<ul class=files>{files}</ul>' if files else '<div class=meta>файлов нет</div>'}"
                f"<div class=score>Оценка 1–5: ____ &nbsp; Замечания: ______________________</div></td>")

    rows = "".join(
        f"<tr><th><b>{html.escape(t['title'])}</b><div class=meta>{html.escape(t['id'])}</div>"
        f"<div class=check><b>Что проверить:</b> {html.escape(t.get('check', ''))}</div></th>"
        + "".join(cell(r.get(e)) for e in ENGINES) + "</tr>"
        for t, r in results)
    total = {e: sum(r[e]["seconds"] for _, r in results if e in r) for e in ENGINES}
    page = f"""<!doctype html><html lang=ru><meta charset=utf-8><title>Сравнение агентов</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#f6f7f9;--card:#fff;--text:#1d2433;--muted:#5d6678;--line:#dde1e8;--err:#c23b3b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14171c;--card:#1d2128;--text:#e6e9ef;--muted:#9aa3b2;--line:#2e333c;--err:#ff7a7a}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,"Segoe UI",sans-serif;padding:16px}}
table{{border-collapse:collapse;width:100%;background:var(--card)}} th,td{{border:1px solid var(--line);padding:10px;vertical-align:top;text-align:left}}
th{{width:22%}} td{{width:39%}} .meta{{color:var(--muted);font-size:12px}} .check{{margin-top:8px;font-weight:normal;font-size:13px}}
.a{{white-space:pre-wrap;max-height:420px;overflow:auto;margin:6px 0}} .q{{color:var(--muted);margin-top:8px}} .err{{color:var(--err)}}
.score{{margin-top:10px;border-top:1px dashed var(--line);padding-top:6px;color:var(--muted)}} .na{{color:var(--muted)}}
</style>
<h1>Сравнение агентов: Hermes и OpenClaw</h1>
<p class=meta>{datetime.now():%d.%m.%Y %H:%M} · тестировщик {html.escape(email)} · всего времени: Hermes {total['hermes']:.0f} с,
OpenClaw {total['openclaw']:.0f} с. Модель и шлюз маскирования — общие.</p>
<table><tr><th>Задача</th><th>Hermes</th><th>OpenClaw</th></tr>{rows}</table></html>"""
    path = outdir / "report.html"
    path.write_text(page, encoding="utf-8")
    (outdir / "results.json").write_text(json.dumps([{"task": t, "result": r} for t, r in results],
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> None:
    args = sys.argv[1:]
    tasks = load_tasks()
    if args[:1] == ["--list"]:
        for t in tasks:
            print(f"{t['id']:22} {t['title']}")
        return
    if not args:
        sys.exit(__doc__)
    email, wanted = args[0].lower(), set(args[1:])
    users = json.loads((DATA / "users.json").read_text(encoding="utf-8"))["users"]
    user = users.get(email) or sys.exit(f"{email}: нет в стенде (users.csv → stand.py up)")
    chosen = [t for t in tasks if not wanted or t["id"] in wanted]
    if wanted - {t["id"] for t in chosen}:
        sys.exit(f"нет задач: {', '.join(sorted(wanted - {t['id'] for t in chosen}))} (compare.py --list)")
    key = env_value("AGENTS_ROUTER_KEY")
    outdir = DATA / "reports" / datetime.now().strftime("%Y-%m-%d_%H%M")
    outdir.mkdir(parents=True, exist_ok=True)
    results = []
    for t in chosen:
        print(f"→ {t['id']}: {t['title']}", flush=True)
        r = run_task(t, email, user, key, outdir)
        for e in ENGINES:
            if e in r:
                print(f"   {TITLES[e]:9} {r[e]['seconds']:6.0f} с  {'ок' if r[e]['ok'] else 'ОШИБКА'}  файлов: {len(r[e]['files'])}")
        results.append((t, r))
        report(results, email, outdir)                 # отчёт обновляется после каждой задачи
    if os.getenv("SUDO_UID"):                          # отчёт — тому, кто запустил через sudo (для scp)
        uid, gid = int(os.environ["SUDO_UID"]), int(os.getenv("SUDO_GID", os.environ["SUDO_UID"]))
        for p in [outdir, *outdir.rglob("*")]:
            os.chown(p, uid, gid)
    print(f"\nОтчёт: {outdir / 'report.html'}\nСкачать на ПК: scp -r <пользователь>@<ВМ>:{outdir} .")


if __name__ == "__main__":
    main()
