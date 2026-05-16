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

    return f"""Тебе нужно применить правку заказчика к статье и сохранить новую версию.

ВХОД:
1. Текущая статья (HTML с шапкой/футером): `{rel_current}`
2. Стайл-гайды: `.claude/style/anti-ai-style.md`, `.claude/style/yandex-quality.md`
3. Правка от заказчика (буквально, как написал):

«{edit_text}»

ЗАДАЧА:
1. Прочитай текущую статью.
2. Прочитай стайл-гайды (anti-ai-style.md, yandex-quality.md) - чтобы новая версия соответствовала им.
3. Прочитай `.claude/agents/4-writer.md` для понимания правил голоса, длины, CTA.
4. Примени правку. Меняй только то, что просит заказчик. Сохраняй:
   - HTML-структуру (header, footer, breadcrumbs, schema.org JSON-LD)
   - CTA-блоки (классы article__cta--hero и article__cta-inline) с тем же topic_action
   - Дисклеймер с копирайтом «Использование материалов сайта возможно только с активной ссылкой на pravo.shop»
   - Голос «мы» (не «я»)
   - 2-3 ссылки на закон максимум
   - Длину тела 6000-7000 знаков (если правка не противоречит)
5. Сохрани результат как `{rel_next}`. Используй Write tool.
6. В конце ответа КРАТКО (1-3 пункта, по одной строке каждый) опиши что именно изменено.
   Этот summary будет показан заказчику в Telegram, поэтому пиши по-человечески, без жаргона.
   Формат строго:

CHANGES_SUMMARY:
- пункт 1
- пункт 2

ВАЖНО:
- Не меняй URL, slug, schema.org @id, breadcrumbs - только если заказчик прямо просит.
- Не выдумывай факты. Если правка противоречит фактам в статье - примени как есть, но в summary пометь это «⚠ потеря факта: ...».
- Не используй длинные тире (—). Только дефис (-) или двоеточие.
- Кавычки только «ёлочки», внутри „лапки".
- Никакого Markdown в summary - просто текст с дефисами в начале строк.
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
        # Ограничение iteration'ов чтобы claude не уходил в долгие циклы анализа.
        # Правка обычно укладывается в 15-25 turns (read + edit + save).
        "--max-turns", "40",
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

    # Коммитим новую версию в git, чтобы Cloud Apps (сайт pravo.shop)
    # подхватила её при redeploy. До этого fix-а 13.05.2026 versions/v*.html
    # оставались untracked на VPS, и превью-роут показывал устаревший v2.0
    # (кейс: правка про отмену госпошлины 300 руб не доходила до заказчика).
    _git_publish_new_version(slug, new_version, next_path)

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
            _git("pull", "--rebase", "origin", "main", timeout=60)
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
