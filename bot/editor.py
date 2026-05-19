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
from pathlib import Path
from typing import NamedTuple

from .config import DRAFTS_DIR, PROJECT_ROOT

log = logging.getLogger(__name__)

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


def _build_prompt(*, slug: str, current_version_path: Path, next_version_path: Path,
                  edit_text: str) -> str:
    """
    Формирует промпт для Claude Code.

    Используем относительные пути от PROJECT_ROOT - так модель легче ориентируется.
    """
    rel_current = current_version_path.relative_to(PROJECT_ROOT).as_posix()
    rel_next = next_version_path.relative_to(PROJECT_ROOT).as_posix()

    return f"""Прими правку заказчика к статье. Работай быстро и минимально.

ВХОД:
1. Текущая статья: `{rel_current}` (стиль и структура УЖЕ ОК, не трогай)
2. Правка от заказчика: «{edit_text}»

ЗАДАЧА (СТРОГО в этом порядке):
1. Прочитай `{rel_current}` (один Read).
2. Скопируй её целиком, измени ТОЛЬКО то, что просит заказчик. Сохрани как `{rel_next}` (один Write).
3. Напиши CHANGES_SUMMARY (1-3 пункта).

НЕ читай никаких стайл-гайдов, .claude/agents/ или других файлов — стиль уже соблюдён в исходнике, твоя задача только применить точечную правку.

СОХРАНИ нетронутыми (если правка их не касается):
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

    prompt = _build_prompt(
        slug=slug,
        current_version_path=current_path,
        next_version_path=next_path,
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
        # Ограничение turns снижено 19.05.2026: правка без чтения стайл-гайдов
        # укладывается в 3-8 turns (Read + Write + summary). 15 — запас.
        "--max-turns", "15",
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
        return EditResult(
            False, None, None, "", None,
            f"Claude Code не ответил за {timeout_sec} сек.",
        )
    except OSError as e:
        return EditResult(False, None, None, "", None, f"Не удалось запустить claude: {e}")

    if proc.returncode != 0:
        stderr_short = (proc.stderr or "").strip().splitlines()[-5:]
        return EditResult(
            False, None, None, "", None,
            f"Claude Code вернул ошибку (код {proc.returncode}): "
            + " | ".join(stderr_short),
        )

    # Парсим JSON-вывод. Формат: {"type": "result", "result": "...текст...", ...}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Возможно вывели несколько JSON-объектов или plain text. Берём как есть.
        result_text = proc.stdout.strip()
    else:
        result_text = data.get("result") or data.get("content") or proc.stdout.strip()

    # Проверяем что новый файл реально создан.
    if not next_path.exists():
        # Возможно Claude сохранил по другому пути или забыл сохранить.
        return EditResult(
            False, None, None, "", None,
            f"Claude не создал файл {next_path.name}. "
            f"Ответ: {result_text[:200]}",
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
