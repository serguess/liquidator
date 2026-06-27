"""
Прямой Python-оркестратор пайплайна на ДВИЖКЕ CLAUDE (подписка Max, без платного API).

=== Зачем (24 июня 2026) ===
Полный проход `claude -p /write-article` (Claude Code оркестрирует 8 субагентов
через Task-tool) стоит ~864K billable-токенов + ~6M cache_read на статью (замер
по `drafts/*/meta.json:tokens_total`). Это НЕ влезает в 5-часовое окно подписки
Max 5x: статьи доходят до черновика, а на пост-обработке ловят «You've hit your
limit» и слот падает (status=failed). Реально выходит 0-2 статьи/сутки вместо 5-10.

Тот же пайплайн как прямой Python-оркестратор (`migration/pipeline_run.py`, вызовы
OpenAI напрямую) стоит ~162K токенов на статью (замер по 39 `_token_report.json`,
median 163K). Разница в ~5 раз — это чистый оркестрационный налог Claude Code:
Task-субагенты, повторные Read файлов в каждом субагенте, tool-definitions и
CLAUDE.md, прокачиваемые в каждый вложенный вызов.

Этот модуль = `pipeline_run.py`, но LLM-движок не OpenAI, а Claude через
`claude -p --model <model>` — ОДИН изолированный headless-вызов на каждый агент
(свой свежий контекст, без Task-вложенности, без накопления истории). Авторизация
идёт через подписку (CLAUDE_CODE_OAUTH_TOKEN в ~/.claude или .env), платный API
не используется. Все детерминированные шаги между агентами переиспользуются 1-в-1
из pipeline_run.py (outline_validate, embed_compare, dedash, pick_scene_template,
inject_boilerplate, quality_gate, image_gen) — здесь ни строчки логики не дублируется.

=== Чем отличается от `claude -p /write-article` ===
- Нет Task-субагентов: оркестрацию (последовательность 1→2→3→4→...) держит этот
  Python-скрипт, а не модель. Модель вызывается голым `claude -p` на один шаг.
- Каждый агент получает РОВНО свой системный промпт + минимально нужный вход,
  а не CLAUDE.md + tool-defs + весь проектный контекст.
- Никаких повторных Read: входные JSON передаются текстом в промпт один раз.
- Детерминированные шаги — обычные python-функции, НЕ вызовы модели.

=== Resilience (24 июня 2026) ===
- StepFailed вместо sys.exit при исчерпании retry: слот получает rc≠0 и берёт
  следующую тему (уже есть в runner), НЕ завершается failed при первом сбое агента.
- Fallback-модели: architect sonnet→haiku при StepFailed (зависание).
- JSON sanitize: попытка вырезать JSON из ответа перед StepFailed.
- STEP_TIMEOUT_SEC=300 (было 600): зависший агент убивается за 5 мин, retry быстрее.

=== Оптимизация токенов (24 июня 2026) ===
- 1-semantics: haiku (было sonnet) — механический JSON по шаблону
- 2-legal-research: haiku (было sonnet) — структурный JSON, знания базовые
- 6-seo-editor: sonnet (было opus) — конвертация MD→HTML, не творческое
- Итог: ~150-200K billable/статью вместо 864K (оркестрационный путь) → 2-5 статей
  за 5-часовое окно Max 5x гарантированно.

=== Запуск (из /home/appuser/apps/liquidator/) ===
    python migration/pipeline_run_claude.py --slug bankrotstvo-fizlica-pri-ipoteke \\
        --category fiz --topic "банкротство физлица при действующей ипотеке" --prod-slug

Для scheduler: runner.py читает CLAUDE_PIPELINE=true и вызывает этот скрипт
вместо `claude --print /write-article`. Аналог GPT_PIPELINE для OpenAI-ветки.

ВАЖНО: sys.exit(rate_limit) даёт runner._is_quota_failure()→circuit breaker.
StepFailed → sys.exit(1) → runner видит rc=1 → hang/failed → берёт следующую тему.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                               # import tools.* (image_gen, dedash, …)
sys.path.insert(0, str(Path(__file__).resolve().parent))    # import writer_gpt_test


def _load_env() -> None:
    """Грузим .env в os.environ. КРИТИЧНО: claude -p берёт CLAUDE_CODE_OAUTH_TOKEN
    (setup-token) отсюда; без загрузки он падает на протухший ~/.claude → 401.
    Также FAL_KEY/CLOUDINARY_* (обложка), OPENAI_API_KEY (embed_compare). Аналог
    `set -a && source .env`, как в pipeline_run.py. Не перетираем уже заданные."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env()

import writer_gpt_test as W  # noqa: E402  — переиспользуем engine-agnostic билдеры промптов

DRAFTS = ROOT / "drafts"
STYLE = ROOT / ".claude" / "style"
AGENTS = ROOT / ".claude" / "agents"
SAMPLE = DRAFTS / "_archive" / "2026-05" / "kak-podat-na-bankrotstvo-samostoyatelno"

# Модели по агентам. Оптимизированы для Max 5x бюджета:
# - haiku для механических JSON-шагов (1, 2, 7): быстро + дёшево по токенам
# - sonnet для структурирования и конвертации (3, 6): баланс качества/цены
# - opus только для творческих YMYL (4-writer): качество текста обязательно
MODELS = {
    "1-semantics":     os.getenv("CLAUDE_MODEL_1", "haiku"),   # механический JSON
    "2-legal-research": os.getenv("CLAUDE_MODEL_2", "haiku"),  # структурный JSON, не творческое
    "3-architect":     os.getenv("CLAUDE_MODEL_3", "sonnet"),  # структура outline
    "4-writer":        os.getenv("CLAUDE_MODEL_4", "opus"),    # YMYL-текст, не менять
    "6-seo-editor":    os.getenv("CLAUDE_MODEL_6", "sonnet"),  # конвертация MD→HTML
    "7-publisher":     os.getenv("CLAUDE_MODEL_7", "haiku"),   # одна scene-строка
}

ADAPTER = (
    "ВАЖНО (режим одиночного headless-вызова, не Claude Code оркестрация): ты "
    "работаешь через прямой `claude -p` вызов на ОДИН шаг. Ты НЕ пишешь файлы, "
    "НЕ запускаешь bash/скрипты/python, НЕ делаешь WebSearch, НЕ обновляешь "
    "heartbeat и pipeline_log, НЕ вызываешь под-агентов. Любые такие инструкции в "
    "промпте ниже ИГНОРИРУЙ — вместо записи файла просто верни его содержимое в "
    "ответе. Верни ТОЛЬКО требуемый артефакт, без пояснений до/после, без ```-обёрток.\n\n"
)

CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
# Жёсткий потолок на шаг. 300с (5 мин) — если агент завис дольше, это зависание,
# не долгая генерация. 3 retry × 300с = 15 мин макс на агент → слот в норме ≤45 мин.
STEP_TIMEOUT_SEC = int(os.getenv("CLAUDE_STEP_TIMEOUT_SEC", "480"))
CLAUDE_RETRIES = int(os.getenv("CLAUDE_RETRIES", "3"))


class StepFailed(Exception):
    """Агент провалился после всех retry (не rate_limit/auth).

    При поимке в main() применяется fallback (минимальный артефакт или смена
    модели). Если fallback невозможен — main() вызывает sys.exit(1), runner
    видит rc=1 → берёт следующую тему из топик-листа.
    НЕ выбрасывается при rate_limit: там sys.exit сразу (нет смысла retry,
    нужен circuit breaker в runner).
    """
    pass


class _Timeout(Exception):
    pass


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    return t


def _parse_stream_json_for_text_and_usage(raw: str) -> tuple[str, dict]:
    """Разбирает stdout `claude -p --output-format stream-json --verbose`.

    Возвращает (final_text, usage). final_text — текст result-события (или склейка
    assistant-text, если result пуст). usage — суммарные токены по всем assistant-
    событиям: {input, output, cache_creation, cache_read, total_billable}.
    """
    final_result = ""
    assistant_parts: list[str] = []
    usage = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        et = ev.get("type")
        if et == "result":
            final_result = str(ev.get("result") or "")
        elif et == "assistant":
            msg = ev.get("message") or {}
            for c in (msg.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    assistant_parts.append(c["text"])
            u = msg.get("usage")
            if isinstance(u, dict):
                usage["input"] += int(u.get("input_tokens") or 0)
                usage["output"] += int(u.get("output_tokens") or 0)
                usage["cache_creation"] += int(u.get("cache_creation_input_tokens") or 0)
                usage["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
    text = final_result if final_result else "\n".join(assistant_parts)
    usage["total_billable"] = usage["input"] + usage["output"] + usage["cache_creation"]
    return text, usage


def _run_claude_once(model: str, system: str, user: str) -> tuple[int, str, float]:
    """Один Popen-вызов claude с hard-timeout STEP_TIMEOUT_SEC.

    NB: watchdog «по тишине потока» НЕ работает для `claude --print` (он не
    стримит промежуточные события, молчит до конца). Поэтому только hard-timeout
    на ВЕСЬ шаг. Зависший агент убивается по STEP_TIMEOUT_SEC.
    """
    # System prompt -> temp file (Linux MAX_ARG_STRLEN = 128KB, writer ~130KB)
    sys_tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", dir=str(ROOT), delete=False, encoding="utf-8",
    )
    sys_tmp.write(system)
    sys_tmp.close()
    cmd = [
        CLAUDE_BIN, "--print",
        "--model", model,
        "--output-format", "stream-json", "--verbose",
        "--append-system-prompt-file", sys_tmp.name,
        "--dangerously-skip-permissions",
    ]
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        proc.stdin.write(user)
        proc.stdin.close()
    except FileNotFoundError:
        os.unlink(sys_tmp.name)
        sys.exit(f"[ERR] claude binary не найден в PATH (CLAUDE_BIN={CLAUDE_BIN})")

    parts: list[str] = []

    def _kill(reason: str):
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.unlink(sys_tmp.name)
        except OSError:
            pass
        raise _Timeout(reason)

    try:
        while True:
            if proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    parts.append(rest)
                break
            try:
                import select as _select
                rlist, _, _ = _select.select([proc.stdout], [], [], 5.0)
                if rlist:
                    line = proc.stdout.readline()
                    if line:
                        parts.append(line)
            except (ImportError, OSError):
                line = proc.stdout.readline()
                if line:
                    parts.append(line)
            if time.time() - t0 > STEP_TIMEOUT_SEC:
                _kill(f"hard timeout >{STEP_TIMEOUT_SEC}s на {model}")
    finally:
        try:
            os.unlink(sys_tmp.name)
        except OSError:
            pass

    return proc.returncode, "".join(parts), time.time() - t0


def claude_chat(model: str, system: str, user: str) -> tuple[str, dict]:
    """Headless-вызов claude с retry. Возвращает (text, usage).

    При исчерпании retry → raise StepFailed (НЕ sys.exit): caller ловит и
    применяет fallback или sys.exit(1) (runner берёт следующую тему).
    При rate_limit/auth → sys.exit немедленно (runner._is_quota_failure → circuit breaker).
    """
    last_err = ""
    for attempt in range(1, CLAUDE_RETRIES + 1):
        try:
            rc, raw, dt = _run_claude_once(model, system, user)
        except _Timeout as e:
            last_err = str(e)
            print(f"   [watchdog] {e} — попытка {attempt}/{CLAUDE_RETRIES}", flush=True)
            continue
        if rc != 0:
            tail = raw[-400:]
            low = raw.lower()
            if any(m in low for m in ("hit your limit", "rate limit", "401", "authentication")):
                # rate_limit: немедленный sys.exit. runner._is_quota_failure ловит
                # строку в stdout и включает circuit breaker.
                sys.exit(f"[ERR] rate_limit: {tail}")
            last_err = f"rc={rc}: {tail}"
            print(f"   [rc={rc}] транзиентный сбой — попытка {attempt}/{CLAUDE_RETRIES}", flush=True)
            continue
        text, usage = _parse_stream_json_for_text_and_usage(raw)
        usage["seconds"] = round(dt, 1)
        usage["attempts"] = attempt
        return _strip_fences(text), usage
    raise StepFailed(f"claude: все {CLAUDE_RETRIES} попыток исчерпаны. Последняя: {last_err}")


def _try_parse_json(content: str) -> dict | None:
    """Попытка распарсить JSON с sanitize. Возвращает dict или None."""
    # Попытка 1: прямой parse
    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        pass
    # Попытка 2: вырезать первый {...} объект из текста (модель добавила пояснения)
    try:
        start = content.index("{")
        end = content.rindex("}") + 1
        return json.loads(content[start:end])
    except (ValueError, json.JSONDecodeError):
        pass
    return None


def parse_json_safe(content: str, who: str, workdir: Path) -> dict:
    """Парсит JSON или raise StepFailed. Сохраняет сырьё для диагностики."""
    d = _try_parse_json(content)
    if d is not None:
        return d
    (workdir / f"_FAILED_{who}.txt").write_text(content, encoding="utf-8")
    raise StepFailed(f"{who} вернул невалидный JSON. Сырьё в _FAILED_{who}.txt")


def agent_prompt(name: str) -> str:
    return W.strip_frontmatter(read(AGENTS / f"{name}.md"))


def save(workdir: Path, fname: str, content: str) -> None:
    (workdir / fname).write_text(content, encoding="utf-8")


def run_py(args: list[str]) -> tuple[int, str]:
    res = subprocess.run([sys.executable, "-m", *args], cwd=str(ROOT),
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def _claude_chat_with_model_fallback(
    primary_model: str, fallback_model: str,
    system: str, user: str,
    agent_name: str,
) -> tuple[str, dict, str]:
    """Пробует primary_model, при StepFailed — fallback_model.
    Возвращает (text, usage, model_used).
    При StepFailed на обоих — raise StepFailed.
    """
    for model in [primary_model, fallback_model]:
        try:
            text, usage = claude_chat(model, system, user)
            return text, usage, model
        except StepFailed as e:
            print(f"   [warn] {agent_name} StepFailed на {model}: {e}", flush=True)
            if model == fallback_model:
                raise
            print(f"   [retry] {agent_name}: пробую fallback {fallback_model}", flush=True)
    raise StepFailed(f"{agent_name}: оба варианта модели исчерпаны")  # unreachable


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--category", default="fiz")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--primary-source", default="")
    ap.add_argument("--event-date", default="")
    ap.add_argument("--news-zone", default="legislation")
    ap.add_argument("--prod-slug", action="store_true",
                    help="писать в drafts/{slug}/ (без _mig-) — для scheduler")
    ap.add_argument("--from-stage", type=int, default=1,
                    help="пропустить stages до N, читать артефакты из drafts/ (для resume)")
    args = ap.parse_args()

    cat = args.category
    real_slug = args.slug
    work_slug = real_slug if args.prod_slug else f"_mig-{real_slug}"
    workdir = DRAFTS / work_slug
    workdir.mkdir(parents=True, exist_ok=True)
    usage_log: list[dict] = []
    sample = (DRAFTS / "edinyj-portal-bankrotstva-fedresurs") if cat == "news" else SAMPLE
    from_stage = args.from_stage

    if from_stage > 1:
        print(f">>> RESUME from stage {from_stage}, reading existing artifacts", flush=True)
        bd = json.loads(read(workdir / "brief.json"))
        rd = json.loads(read(workdir / "research.json"))

    def step(name: str, system: str, user: str, json_mode_hint: bool = False,
             no_reasoning: bool = False) -> str:
        model = MODELS[name]
        print(f"\n=== {name} (claude {model}) ===", flush=True)
        sys_full = ADAPTER + system
        if json_mode_hint:
            user = user + "\n\nВерни СТРОГО валидный JSON-объект и больше ничего."
        if no_reasoning:
            user = user + (
                "\n\nВыведи финальный артефакт за ОДНУ генерацию. НЕ выводи в ответе "
                "пошаговые рассуждения, планирование вслух, черновые варианты и "
                "повторные самопроверки — только готовый результат.")
        content, usage = claude_chat(model, sys_full, user)
        usage["agent"] = name
        usage["model"] = model
        usage_log.append(usage)
        print(f"   ok: {len(content)} симв., usage in={usage['input']} out={usage['output']} "
              f"cache_w={usage['cache_creation']} cache_r={usage['cache_read']} "
              f"billable={usage['total_billable']} ток, {usage['seconds']}с")
        return content

    if from_stage <= 5:
        # ---- 1. semantics → brief.json ----
        sys1 = agent_prompt("1-semantics") + f"\n\n=== ОБРАЗЕЦ ФОРМАТА brief.json ===\n{read(sample/'brief.json')}"
        news_hint = ""
        if cat == "news":
            news_hint = (
                f"\nЭто NEWS-тема. ОБЯЗАТЕЛЬНО заполни в brief: event_date=\"{args.event_date}\", "
                f"news_zone=\"{args.news_zone}\", primary_source=\"{args.primary_source}\". "
                "Длина news 4500-6500 знаков. Фокус на конкретном событии/изменении, не общий гайд.")
        usr1 = (f"category={cat}\nslug={real_slug}\nТема: {args.topic}{news_hint}\n\n"
                f"Верни brief.json для этой темы (slug строго '{real_slug}'). "
                "Каннибализацию и WebSearch пропусти. Только валидный JSON.")
        # Агент 1 — rate_limit и topic-reject должны дойти до runner без перехвата.
        # StepFailed здесь = нет смысла продолжать (нет brief → нет всего остального).
        try:
            brief = step("1-semantics", sys1, usr1, json_mode_hint=True)
            bd = parse_json_safe(brief, "brief", workdir)
        except StepFailed as e:
            print(f"[ERR] 1-semantics failed: {e}", flush=True)
            sys.exit(1)
        bd["slug"] = real_slug; bd["category"] = cat
        save(workdir, "brief.json", json.dumps(bd, ensure_ascii=False, indent=2))

        # ---- 2. legal-research → research.json ----
        sys2 = (agent_prompt("2-legal-research")
                + f"\n\n=== ИСТОЧНИК ПРАВДЫ ПО ФАКТАМ (legal-facts.md) ===\n{read(STYLE/'legal-facts.md')}"
                + f"\n\n=== ОБРАЗЕЦ ФОРМАТА research.json ===\n{read(sample/'research.json')}")
        usr2 = (f"brief.json:\n{json.dumps(bd, ensure_ascii=False)}\n\n"
                "WebSearch отключён — используй знания + legal-facts.md. Верни research.json (валидный JSON).")
        try:
            research = step("2-legal-research", sys2, usr2, json_mode_hint=True)
            rd = parse_json_safe(research, "research", workdir)
        except StepFailed as e:
            # Fallback: минимальный research чтобы architect/writer продолжили.
            # Качество снизится, но статья выйдет и не сожжёт слот.
            print(f"   [warn] legal-research failed ({e}) — minimal fallback", flush=True)
            rd = {
                "fallback": True,
                "legal_basis": [],
                "key_facts": [],
                "main_keyword": bd.get("main_keyword", args.topic),
                "category": cat,
            }
        save(workdir, "research.json", json.dumps(rd, ensure_ascii=False, indent=2))

        # ---- 3. architect → outline.json + детерминированная валидация ----
        pub_index = read(ROOT / "data" / "published_index.json")[:6000]
        sys3 = agent_prompt("3-architect") + f"\n\n=== ОБРАЗЕЦ ФОРМАТА outline.json ===\n{read(sample/'outline.json')}"
        usr3 = (f"brief.json:\n{json.dumps(bd, ensure_ascii=False)}\n\n"
                f"research.json:\n{json.dumps(rd, ensure_ascii=False)}\n\n"
                f"published_index (для перелинковки, фрагмент):\n{pub_index}\n\n"
                "Верни outline.json с topic_terms и лексическими зонами (валидный JSON).")
        try:
            # no_reasoning=True убирает вывод пошаговых рассуждений — главный драйвер
            # зависания architect. sonnet первым, haiku fallback при timeout.
            outline_raw, arch_usage, arch_model = _claude_chat_with_model_fallback(
                primary_model=MODELS["3-architect"],
                fallback_model="haiku",
                system=ADAPTER + sys3 + "\n\nВерни СТРОГО валидный JSON-объект и больше ничего."
                       + "\n\nВыведи финальный артефакт за ОДНУ генерацию. НЕ выводи в ответе "
                       "пошаговые рассуждения, планирование вслух, черновые варианты и "
                       "повторные самопроверки — только готовый результат.",
                user=usr3,
                agent_name="3-architect",
            )
            arch_usage["agent"] = "3-architect"; arch_usage["model"] = arch_model
            usage_log.append(arch_usage)
            print(f"   ok: {len(outline_raw)} симв., billable={arch_usage['total_billable']} ток "
                  f"(model={arch_model})")
            od = parse_json_safe(_strip_fences(outline_raw), "outline", workdir)
        except StepFailed as e:
            print(f"[ERR] 3-architect failed на всех моделях: {e}", flush=True)
            sys.exit(1)
        save(workdir, "outline.json", json.dumps(od, ensure_ascii=False, indent=2))
        rc, _ = run_py(["tools.outline_validate", f"drafts/{work_slug}/outline.json", "--fix"])
        print(f"   [outline_validate] exit={rc}")

        # ---- 4. writer → draft.md ----
        print(f"\n=== 4-writer (claude {MODELS['4-writer']}) ===", flush=True)
        wsys = ADAPTER + W.build_system_prompt()
        wuser = W.build_user_prompt(work_slug, cat)
        try:
            draft, wusage = claude_chat(MODELS["4-writer"], wsys, wuser)
        except StepFailed as e:
            print(f"[ERR] 4-writer failed: {e}", flush=True)
            sys.exit(1)
        wusage["agent"] = "4-writer"; wusage["model"] = MODELS["4-writer"]
        usage_log.append(wusage)
        save(workdir, "draft.md", draft)
        print(f"   ok: {len(draft)} симв., billable={wusage['total_billable']} ток, {wusage['seconds']}с")

        # self-fix 1 проход (cap=2)
        rep = W.run_quality_checks(workdir / "draft.md")
        if rep:
            m = W.extract(rep)
            if not (m["pred_spam"] <= 50 and m["lex"] >= 0.62 and m["length_status"] == "ok"):
                print("   [writer self-fix проход 2]")
                fix_user = wuser + "\n\n" + W.build_feedback(m) + (
                    "\n\nВыше — твой предыдущий черновик НЕ дотянул по числам. Перепиши "
                    "точечно по списку и верни ПОЛНЫЙ обновлённый draft.md тем же форматом."
                    "\n\n=== ТВОЙ ПРЕДЫДУЩИЙ DRAFT ===\n" + draft)
                try:
                    draft, wusage2 = claude_chat(MODELS["4-writer"], wsys, fix_user)
                except StepFailed as e:
                    print(f"   [warn] writer self-fix failed ({e}) — оставляю первый черновик",
                          flush=True)
                    wusage2 = None
                if wusage2:
                    wusage2["agent"] = "4-writer(fix)"; wusage2["model"] = MODELS["4-writer"]
                    usage_log.append(wusage2)
                    save(workdir, "draft.md", draft)
                    print(f"   ok: billable={wusage2['total_billable']} ток")

        # ---- 5. uniqueness (детерминированный) ----
        print("\n=== 5-uniqueness (embed_compare) ===", flush=True)
        rc5, out5 = run_py(["tools.embed_compare", work_slug])
        if out5.strip() and "{" in out5:
            uniq = _try_parse_json(out5[out5.find("{"):out5.rfind("}") + 1])
            if uniq:
                save(workdir, "uniqueness.json", json.dumps(uniq, ensure_ascii=False, indent=2))
                print(f"   passed={uniq.get('passed')} scores={uniq.get('scores')}")
            else:
                print(f"   [warn] uniqueness parse failed; tail: {out5[-200:]}")
        else:
            print(f"   [warn] embed_compare без JSON (rc={rc5})")

    else:
        print("=== Stages 1-5 SKIPPED (resume) ===", flush=True)

    # ---- 6. seo-editor → body.html + meta.json ----
    sys6 = (agent_prompt("6-seo-editor")
            + f"\n\n=== editor-cheatsheet.md ===\n{read(STYLE/'editor-cheatsheet.md')}"
            + f"\n\n=== ОБРАЗЕЦ ФОРМАТА meta.json ===\n{read(sample/'meta.json')}")
    usr6 = (f"draft.md:\n{read(workdir/'draft.md')}\n\n"
            f"brief.json:\n{json.dumps(bd, ensure_ascii=False)}\n\n"
            f"research.json:\n{json.dumps(rd, ensure_ascii=False)}\n\n"
            "Сконвертируй draft в HTML тела статьи и заполни meta. Верни ОДИН JSON-объект: "
            '{"meta": {...все поля meta.json...}, "body_html": "<...HTML тела...>"}. '
            "Валидный JSON.\n\n"
            "ПРАВИЛА body_html (СТРОГО, частые дефекты):\n"
            "- ТОЛЬКО теги тела: <p>, <h2>, <h3>, <ul>/<ol>/<li>, <strong>, <a>. "
            "БЕЗ <html>/<head>/<body>/<article>/<header>/<footer> — это даст boilerplate.\n"
            "- НЕ добавляй и УДАЛИ из тела, если есть в draft: дисклеймер, копирайт "
            "«© ООО», блок «Об авторе», блок «Читайте также», готовые HTML-кнопки CTA. "
            "Всё это вставляет boilerplate автоматически — дубли запрещены.\n"
            "- BP-маркеры из draft.md (<!--BP:CTA-TOP-->, <!--BP:CTA-MID-->, "
            "<!--BP:CTA-BOTTOM-->, <!--BP:DISCLAIMER-->) перенеси на их места как "
            "HTML-комментарии БЕЗ изменений — НЕ заменяй их текстом/кнопками.\n"
            "- Сохрани структуру H2/H3 из draft КАК ЕСТЬ: где в draft есть H3-подразделы, "
            "оставь их H3, не схлопывай в список и не повышай до H2.\n"
            "- Сохрани все авторские вставки «мы» («по нашему опыту» и т.п.) из draft "
            "дословно — не вычищай их.\n"
            "- Сохрани ПОРЯДОК BP-маркеров как в draft: CTA-BOTTOM и DISCLAIMER идут "
            "ДО блока «Частые вопросы», FAQ — последний раздел. Не переставляй их после FAQ.")
    try:
        seo_raw, seo_usage, seo_model = _claude_chat_with_model_fallback(
            primary_model=MODELS["6-seo-editor"],
            fallback_model="haiku",
            system=ADAPTER + sys6 + "\n\nВерни СТРОГО валидный JSON-объект и больше ничего.",
            user=usr6,
            agent_name="6-seo-editor",
        )
        seo_usage["agent"] = "6-seo-editor"; seo_usage["model"] = seo_model
        usage_log.append(seo_usage)
        print(f"   ok: {len(seo_raw)} симв., billable={seo_usage['total_billable']} ток "
              f"(model={seo_model})")
        sd = parse_json_safe(_strip_fences(seo_raw), "seo", workdir)
    except StepFailed as e:
        print(f"[ERR] 6-seo-editor failed: {e}", flush=True)
        sys.exit(1)

    from tools.dedash import dedash
    sd = dedash(sd)
    meta = sd.get("meta", {}) if isinstance(sd, dict) else {}
    meta["slug"] = real_slug; meta["category"] = cat
    save(workdir, "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    save(workdir, "body.html", sd.get("body_html", ""))

    # ---- 7b. pick_scene_template (детерминированная ротация) ----
    rc, _ = run_py(["articles_scheduler.pick_scene_template", work_slug, cat])
    print(f"   [pick_scene_template] exit={rc}")

    # ---- 7. publisher → scene.txt ----
    tmpl = read(workdir / "scene_template.txt").strip()
    sys7 = agent_prompt("7-publisher") + f"\n\n=== cover-scenes.md ===\n{read(STYLE/'cover-scenes.md')}"
    usr7 = (f"Тема статьи: {bd.get('title') or args.topic}, категория {cat}.\n"
            f"Выбранный шаблон: {tmpl or '(нет — выбери уместный сам)'}\n"
            "Найди этот template_id в cover-scenes.md, адаптируй под смысл статьи "
            "(подбери 3-7 предметов из allowed pool). Верни ТОЛЬКО английскую scene-строку "
            "для генерации обложки (без JSON, без пояснений).")
    try:
        scene = step("7-publisher", sys7, usr7)
        save(workdir, "scene.txt", scene)
    except StepFailed as e:
        # Нет scene → image_gen возьмёт CATEGORY_SCENE_DEFAULT; продолжаем.
        print(f"   [warn] 7-publisher failed ({e}) — default scene", flush=True)
        scene = ""

    # ---- ОБЛОЖКА ----
    print("\n=== обложка (image_gen: fal.ai + Cloudinary) ===", flush=True)
    try:
        from tools.image_gen import generate_and_upload_cover
        cover = generate_and_upload_cover(
            slug=work_slug, title=meta.get("title") or args.topic,
            category=cat, scene=(scene.strip() or None), write_meta=True)
        print(f"   cover_url: {cover}")
    except Exception as e:
        print(f"   [image_gen failed] {type(e).__name__}: {e}")

    # ---- inject_boilerplate → article.html ----
    rc, _ = run_py(["tools.inject_boilerplate", f"drafts/{work_slug}/",
                    "--body", "body.html", "--out", "article.html"])
    print(f"   [inject_boilerplate] exit={rc}")

    # ---- quality_gate ----
    rc, _ = run_py(["tools.quality_gate", f"drafts/{work_slug}/article.html",
                    "--json", "--save-report"])
    print(f"\n[quality_gate] exit={rc} (0=passed)")

    # ====== ОТЧЁТ ПО ТОКЕНАМ ======
    print("\n" + "=" * 90)
    print(f"ОТЧЁТ ПО ТОКЕНАМ (CLAUDE-движок) — статья '{real_slug}' ({cat})")
    print("=" * 90)
    print(f"{'агент':<20}{'модель':<10}{'in':>9}{'out':>8}{'cache_w':>9}{'cache_r':>10}{'billable':>10}")
    tot = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "total_billable": 0}
    for u in usage_log:
        for k in tot:
            tot[k] += u.get(k, 0)
        print(f"{u['agent']:<20}{u['model']:<10}{u['input']:>9}{u['output']:>8}"
              f"{u['cache_creation']:>9}{u['cache_read']:>10}{u['total_billable']:>10}")
    print("-" * 90)
    print(f"{'ИТОГО':<20}{'':<10}{tot['input']:>9}{tot['output']:>8}"
          f"{tot['cache_creation']:>9}{tot['cache_read']:>10}{tot['total_billable']:>10}")
    print(f"\nBillable-токенов на статью: ~{tot['total_billable']//1000}K "
          f"(cache_read {tot['cache_read']//1000}K не считается в лимит подписки).")
    save(workdir, "_token_report_claude.json",
         json.dumps({"agents": usage_log, "totals": tot}, ensure_ascii=False, indent=2))
    print(f"Артефакты: drafts/{work_slug}/  | quality_gate exit={rc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
