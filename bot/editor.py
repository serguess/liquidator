"""
Editor: применяет правку заказчика к статье через Claude Code (subprocess).

Claude Code должен быть установлен и авторизован на сервере:
    npm install -g @anthropic-ai/claude-code
    # авторизация: ANTHROPIC_API_KEY в env, либо ~/.claude.json скопирован
    # с локальной машины

Вызов:
    claude -p "<промпт>" \
      --output-format json \
      --dangerously-skip-permissions \
      --add-dir <project_root>

Возвращает JSON в stdout с полем "result" (текст финального ответа модели).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from .config import DRAFTS_DIR, PROJECT_ROOT

log = logging.getLogger(__name__)

# Retry при exit!=0 (не timeout). Покрывает transient-ошибки claude CLI:
# overloaded (529), rate-limit (429), конфликт двух параллельных claude при
# активном scheduler (Max-план может отклонять вторую сессию). Часто 2-я
# попытка через паузу проходит мгновенно — без ожидания освобождения слота.
EDIT_MAX_ATTEMPTS = 3
EDIT_RETRY_BACKOFF_SEC = (8, 20)  # паузы перед попыткой 2 и 3

# Полные логи сбойных edit-вызовов — для диагностики «exit 1 с пустым stderr».
EDIT_LOGS_DIR = PROJECT_ROOT / "data" / "edit_logs"

# Изолированный HOME для edit-claude. Без этого edit-claude и scheduler-claude
# делят /home/appuser/.claude.json и /home/appuser/.claude/ — при параллельных
# запусках второй процесс висит на чтении lockfile, упирается в 360-сек
# timeout subprocess.run и заказчик видит "❌ Не удалось применить правку".
# Реальный кейс 12 мая 2026: пока scheduler писал статью, заказчик два раза
# подряд не смог отредактировать другую.
#
# /tmp выбран намеренно: при PrivateTmp=true в systemd unit это приватный
# tmpfs для bot-сервиса, scheduler в свой /tmp не достанет. Содержимое
# теряется при рестарте бота, но это OK — claude перерегистрируется по
# CLAUDE_CODE_OAUTH_TOKEN из env.
EDITOR_HOME = Path("/tmp/claude-editor-home")


class EditResult(NamedTuple):
    success: bool
    new_version: str | None
    new_html_path: Path | None
    summary: str
    char_count: int | None
    error: str | None


def _next_version(versions: list[str]) -> str:
    """
    Из ["2.0", "2.1"] вернёт "2.2".
    Если versions пуст или невалидно - "2.0".
    """
    nums = []
    for v in versions:
        try:
            major, minor = v.split(".", 1)
            nums.append((int(major), int(minor)))
        except (ValueError, AttributeError):
            continue
    if not nums:
        return "2.0"
    nums.sort()
    last_major, last_minor = nums[-1]
    return f"{last_major}.{last_minor + 1}"


def _check_claude_available() -> str | None:
    """Возвращает None если всё ок, или текст ошибки."""
    if shutil.which("claude") is None:
        return (
            "Не найден бинарник 'claude' в PATH. "
            "Установите: npm install -g @anthropic-ai/claude-code"
        )
    return None


def _build_prompt(*, slug: str, target_path: Path, edit_text: str) -> str:
    """
    Формирует промпт для Claude Code.

    Файл уже скопирован Python-кодом в target_path. Claude только Edit-ит diff,
    не переписывая весь файл (экономия ~4 мин на 41 КБ HTML).
    """
    rel_target = target_path.relative_to(PROJECT_ROOT).as_posix()

    return f"""Прими правку заказчика к статье. Работай быстро и минимально.

ВХОД:
1. Файл статьи: `{rel_target}` (уже скопирован, редактируй его на месте)
2. Правка от заказчика: «{edit_text}»

ЗАДАЧА (СТРОГО в этом порядке):
1. Прочитай `{rel_target}` (один Read).
2. Примени ТОЛЬКО то, что просит заказчик, через Edit (точечная замена фрагментов). НЕ используй Write - файл уже готов, меняй только нужные куски.
3. Напиши CHANGES_SUMMARY (1-3 пункта).

НЕ читай никаких стайл-гайдов, .claude/agents/ или других файлов.

НЕ ТРОГАЙ (если правка их не касается):
- HTML-структуру (header, footer, breadcrumbs, schema.org JSON-LD)
- CTA-блоки (article__cta--hero, article__cta-inline)
- Дисклеймер с копирайтом
- URL, slug, @id

Формат summary:

CHANGES_SUMMARY:
- пункт 1
- пункт 2

ПИШИ: дефис (-) вместо длинного тире (—). Кавычки «ёлочки». Без Markdown в summary.
"""


def _parse_summary(claude_output: str) -> str:
    """
    Достаёт блок CHANGES_SUMMARY из ответа Claude Code.
    Если не нашёл - возвращает первые 3 строки или весь ответ.
    """
    m = re.search(
        r"CHANGES_SUMMARY:\s*\n(.+?)(?:\n\n|$)",
        claude_output,
        flags=re.DOTALL,
    )
    if m:
        body = m.group(1).strip()
        # Чистим до первого разрыва или конца текста.
        lines = [l.rstrip() for l in body.splitlines() if l.strip()]
        return "\n".join(lines[:6])  # максимум 6 пунктов

    # Fallback - первые осмысленные строки.
    lines = [l.strip() for l in claude_output.splitlines() if l.strip()]
    if not lines:
        return "Изменения применены."
    return "\n".join(f"- {l}" for l in lines[:3])


def _count_html_chars(html_path: Path) -> int:
    """
    Считает символы авторского текста ТАКЖЕ как quality_gate (tools/quality_checks):
    только содержимое <article>...</article>, без header/footer/CTA/JSON-LD/FAQ-вопросов.
    Раньше считали весь body — цифра была на 2-3 тысячи больше реальной.
    """
    try:
        from tools.quality_checks import extract_author_text_from_html
    except ImportError:
        extract_author_text_from_html = None
    try:
        text = html_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    if extract_author_text_from_html:
        return len(extract_author_text_from_html(text))
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<head\b[^>]*>.*?</head>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return len(text.strip())


def _extract_claude_error(stdout: str, stderr: str) -> str:
    """claude --output-format json пишет ошибку в STDOUT (а не stderr): объект
    с is_error=true и текстом, либо subtype вроде 'error_max_turns'. Достаём
    человекочитаемую причину. Раньше код смотрел только пустой stderr — поэтому
    «exit 1 с пустым stderr» был непрозрачным."""
    out = (stdout or "").strip()
    if out:
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                # claude CLI result-объект
                for key in ("error", "result", "subtype", "message"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()[:300]
                if data.get("is_error"):
                    return f"is_error=true ({data.get('subtype', 'unknown')})"
        except json.JSONDecodeError:
            # не JSON — вернём хвост stdout (там может быть стектрейс/сообщение)
            return out.splitlines()[-1][:300]
    err = (stderr or "").strip()
    if err:
        return " | ".join(err.splitlines()[-3:])[:300]
    return ""


def _save_edit_failure_log(slug: str, attempt: int, returncode: int,
                           stdout: str, stderr: str) -> None:
    """Сохраняет полный вывод сбойного edit-вызова + снимок памяти в
    data/edit_logs/<ts>_<slug>.json. Нужно для диагностики редких крашей при
    активном scheduler (поймать реальную причину: overloaded/rate-limit/OOM)."""
    try:
        EDIT_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        meminfo = ""
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")[:600]
        except OSError:
            pass
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload = {
            "ts_utc": ts, "slug": slug, "attempt": attempt,
            "returncode": returncode,
            "stdout": (stdout or "")[:4000],
            "stderr": (stderr or "")[:4000],
            "meminfo_head": meminfo,
        }
        (EDIT_LOGS_DIR / f"{ts}_{slug}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("Не смог записать edit-failure лог: %s", exc)


def apply_edit(*, slug: str, current_version: str, versions: list[str],
               edit_text: str, timeout_sec: int = 360) -> EditResult:
    """
    Применяет правку. Создаёт новую версию в drafts/{slug}/versions/v{next}.html.

    Параметры:
        slug             - папка драфта в drafts/
        current_version  - например "2.0" или "2.1"
        versions         - список всех известных версий из state, для расчёта next
        edit_text        - текст правки от заказчика (буквально)
    """
    avail_err = _check_claude_available()
    if avail_err:
        return EditResult(False, None, None, "", None, avail_err)

    folder = DRAFTS_DIR / slug
    versions_dir = folder / "versions"
    versions_dir.mkdir(exist_ok=True)

    current_path = versions_dir / f"v{current_version}.html"
    if not current_path.exists():
        # Fallback: если текущая версия пропала, попробуем v2.0 или article-v2.html.
        fallback = versions_dir / "v2.0.html"
        if not fallback.exists():
            fallback = folder / "article-v2.html"
        if not fallback.exists():
            return EditResult(
                False, None, None, "", None,
                f"Не найдена текущая версия ({current_version}) и нет fallback'а.",
            )
        current_path = fallback

    new_version = _next_version(versions)
    next_path = versions_dir / f"v{new_version}.html"

    # Pre-copy: Python копирует файл, Claude только Edit-ит diff.
    # Экономит ~4 мин: модель не переписывает 41 КБ HTML через Write.
    try:
        shutil.copy2(current_path, next_path)
    except OSError as e:
        return EditResult(
            False, None, None, "", None,
            f"Не удалось скопировать {current_path.name} → {next_path.name}: {e}",
        )

    prompt = _build_prompt(
        slug=slug,
        target_path=next_path,
        edit_text=edit_text,
    )

    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--add-dir", str(PROJECT_ROOT),
        # Sonnet вместо дефолтного opus: правки одного блока не требуют opus-качества,
        # sonnet в 3-5 раз быстрее. Снижает время правки с 8-12 мин до 2-3 мин.
        # Фикс 16.05.2026.
        "--model", "sonnet",
        # Pre-copy + Edit: 3-4 turns (Read + 1-2 Edit + summary). 8 — запас.
        "--max-turns", "8",
    ]

    # Готовим изолированный env с собственным HOME — иначе edit-claude конфликтует
    # с активным scheduler-claude через ~/.claude.json и висит на 360-сек timeout.
    try:
        EDITOR_HOME.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Не смог создать %s: %s — fallback на общий HOME",
                    EDITOR_HOME, exc)
    edit_env = os.environ.copy()
    edit_env["HOME"] = str(EDITOR_HOME)

    # Retry-loop: exit!=0 часто transient (overloaded/rate-limit/конфликт с
    # активным scheduler). Повтор через паузу обычно проходит — без ожидания
    # освобождения слота. Timeout НЕ повторяем (это и так слишком долго).
    proc = None
    for attempt in range(1, EDIT_MAX_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                cwd=str(PROJECT_ROOT),
                encoding="utf-8",
                env=edit_env,
            )
        except subprocess.TimeoutExpired:
            next_path.unlink(missing_ok=True)
            return EditResult(
                False, None, None, "", None,
                f"Claude Code не ответил за {timeout_sec} сек.",
            )
        except OSError as e:
            next_path.unlink(missing_ok=True)
            return EditResult(False, None, None, "", None, f"Не удалось запустить claude: {e}")

        if proc.returncode == 0:
            break

        # exit != 0 — диагностика + (возможно) retry
        reason = _extract_claude_error(proc.stdout, proc.stderr)
        _save_edit_failure_log(slug, attempt, proc.returncode, proc.stdout, proc.stderr)
        log.warning(
            "Edit attempt %d/%d упал: slug=%s code=%d reason=%r",
            attempt, EDIT_MAX_ATTEMPTS, slug, proc.returncode, reason[:200],
        )
        if attempt < EDIT_MAX_ATTEMPTS:
            backoff = EDIT_RETRY_BACKOFF_SEC[min(attempt - 1, len(EDIT_RETRY_BACKOFF_SEC) - 1)]
            time.sleep(backoff)
            continue

        # Исчерпали попытки — возвращаем осмысленную причину (из stdout, не пустой stderr)
        next_path.unlink(missing_ok=True)
        detail = f": {reason}" if reason else " (без деталей — см. data/edit_logs/)"
        return EditResult(
            False, None, None, "", None,
            f"Claude Code вернул ошибку (код {proc.returncode}){detail}",
        )

    # Парсим JSON-вывод. Формат: {"type": "result", "result": "...текст...", ...}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Возможно вывели несколько JSON-объектов или plain text. Берём как есть.
        result_text = proc.stdout.strip()
    else:
        result_text = data.get("result") or data.get("content") or proc.stdout.strip()

    # Проверяем что файл был изменён (pre-copy гарантирует его существование,
    # но Claude мог не применить Edit - тогда файл идентичен исходнику).
    if not next_path.exists():
        return EditResult(
            False, None, None, "", None,
            f"Файл {next_path.name} пропал после вызова Claude. "
            f"Ответ: {result_text[:200]}",
        )
    if next_path.read_bytes() == current_path.read_bytes():
        next_path.unlink(missing_ok=True)
        return EditResult(
            False, None, None, "", None,
            f"Claude не внёс изменений в файл. Ответ: {result_text[:200]}",
        )

    summary = _parse_summary(result_text)
    char_count = _count_html_chars(next_path)

    # Git push в ФОНЕ (с 19.05.2026): заказчик получает сообщение «версия готова»
    # сразу после claude, не ждёт сетевых git-операций (экономия 1-3 сек).
    # На Timeweb test.pravo.shop отдаёт versions/v*.html сразу из local FS — git
    # нужен только для бэкапа + sync с Cloud Apps (всё фоном).
    # daemon=True: если бот рестартнётся, thread прерывается; в этом случае
    # untracked файл подхватится следующим pull --rebase --autostash от scheduler.
    threading.Thread(
        target=_git_publish_new_version,
        args=(slug, new_version, next_path),
        daemon=True,
        name=f"git-push-edit-{slug}-v{new_version}",
    ).start()

    return EditResult(
        success=True,
        new_version=new_version,
        new_html_path=next_path,
        summary=summary,
        char_count=char_count,
        error=None,
    )


def _git_publish_new_version(slug: str, version: str, file_path: Path) -> None:
    """
    Коммитит новую версию HTML и пушит на origin/main.

    Если git операции падают (auth, network, конфликт со scheduler) —
    логируем и идём дальше. Правка для заказчицы уже применена локально
    в drafts/, она увидит «применено» в TG. На следующем scheduler-тике
    в любом случае пройдёт git pull/rebase и наш необкоммиченный/
    необпушенный файл подхватится с retry.

    pull --rebase перед push защищает от гонки со scheduler.

    --autostash обязателен: scheduler оставляет в рабочем дереве unstaged
    изменения (writer/seo-editor пишут в drafts/), и без autostash
    `pull --rebase` падает с «cannot pull with rebase: You have unstaged
    changes», edit-коммит остаётся локальным → Cloud Apps файла v2.X не
    видит → заказчик кликает ссылку из «✏️ Версия готова» и получает
    старую v2.0 через fallback на article-v2.html. Реальный кейс
    17.05.2026: правка про порог 2 млн руб не дошла до сайта.
    """
    rel_path = file_path.relative_to(PROJECT_ROOT).as_posix()
    msg = f"edit({slug}): apply v{version}"

    def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )

    try:
        _git("add", rel_path)
        _git("commit", "-m", msg)
        try:
            _git("pull", "--rebase", "--autostash", "origin", "main", timeout=60)
        except subprocess.CalledProcessError as e:
            log.warning(
                "Edit git: pull --rebase failed, aborting rebase. stderr=%s",
                (e.stderr or "")[:300],
            )
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                timeout=15,
            )
            return
        _git("push", "origin", "main", timeout=60)
        log.info("Edit pushed: slug=%s version=%s commit=%r", slug, version, msg)
    except subprocess.CalledProcessError as e:
        log.error(
            "Edit git publish failed (slug=%s v=%s): %s | stderr=%s",
            slug, version, e, (e.stderr or "")[:500],
        )
    except subprocess.TimeoutExpired as e:
        log.error("Edit git publish timeout (slug=%s v=%s): %s", slug, version, e)
    except OSError as e:
        log.error("Edit git publish OS error: %s", e)
