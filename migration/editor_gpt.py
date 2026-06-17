"""
GPT-редактор статьи (миграция кнопки «Править» с Anthropic на OpenAI).

Заменяет subprocess-вызов `claude -p --model sonnet` в bot/editor.py одним
вызовом gpt-5-mini. Заказчик жмёт «Править» → пишет правку → модель применяет
её точечно ко ВСЕМУ HTML и возвращает целиком отредактированный файл +
краткое summary. Не зависит от claude login (он на сервере разлогинен).

Контракт совместим с bot.editor.apply_edit: apply_edit_gpt(...) возвращает
тот же EditResult и так же кладёт новую версию в drafts/{slug}/versions/.

Почему весь файл, а не diff: через chat.completions нет инструмента Edit,
поэтому модель возвращает полный HTML. Стоимость ~$0.03-0.04/правка (gpt-5-mini),
есть проверки на обрыв/потерю контента.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import writer_gpt_test as W  # noqa: E402

DEFAULT_MODEL = "gpt-5-mini"
SUMMARY_MARK = "<<<SUMMARY>>>"


class EditError(Exception):
    """Понятная для заказчика причина, почему правка не применилась."""


def _dedash(s: str) -> str:
    """Длинное/среднее тире → дефис (правило стиля заказчицы)."""
    return s.replace("—", "-").replace("–", "-")


def _build_system() -> str:
    return (
        "Ты редактор готовой HTML-статьи о банкротстве. Применяешь ТОЧЕЧНУЮ "
        "правку заказчика и возвращаешь файл ЦЕЛИКОМ.\n\n"
        "ЖЁСТКИЕ ПРАВИЛА:\n"
        "1. Верни ВЕСЬ HTML-документ от первого до последнего символа, изменив "
        "ТОЛЬКО то, что прямо просит заказчик. Остальное — байт-в-байт как было.\n"
        "2. НЕ ТРОГАЙ (если правка их не касается): HTML-структуру, <head>, "
        "schema.org JSON-LD, header/footer/breadcrumbs, CTA-блоки "
        "(article__cta--hero, article__cta-inline), дисклеймер с копирайтом, "
        "URL, slug, @id, мета-теги.\n"
        "3. Длинное тире (—) запрещено — только дефис (-). Кавычки «ёлочки».\n"
        "4. НЕ оборачивай ответ в markdown ``` и не добавляй пояснений.\n\n"
        "ФОРМАТ ОТВЕТА строго такой:\n"
        "<полный HTML>\n"
        f"{SUMMARY_MARK}\n"
        "- что изменил, пункт 1\n"
        "- пункт 2 (если есть)\n"
    )


def edit_html_gpt(original_html: str, edit_text: str, *,
                  model: str = DEFAULT_MODEL, timeout_sec: int = 200) -> tuple[str, str]:
    """Возвращает (new_html, summary). Бросает EditError при проблеме."""
    from openai import OpenAI, APIConnectionError, APITimeoutError, RateLimitError

    client = OpenAI(api_key=W.load_api_key(), timeout=float(timeout_sec), max_retries=0)
    system = _build_system()
    user = (
        f"ПРАВКА ЗАКАЗЧИКА: «{edit_text}»\n\n"
        "Примени её к статье ниже и верни весь HTML целиком, затем "
        f"{SUMMARY_MARK} и 1-3 пункта.\n\n"
        "=== HTML СТАТЬИ ===\n" + original_html
    )
    kwargs = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_completion_tokens": 32000,
        "reasoning_effort": "low",
    }
    resp = None
    last_exc = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(**kwargs)
            break
        except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
            last_exc = exc
            time.sleep(5 * (attempt + 1))
    if resp is None:
        raise EditError(f"OpenAI недоступен после ретраев: {type(last_exc).__name__}")

    choice = resp.choices[0]
    content = (choice.message.content or "").strip()
    if choice.finish_reason == "length":
        raise EditError("Ответ модели обрезан по длине (статья слишком большая).")
    if not content:
        raise EditError("Пустой ответ модели.")

    # снять возможные ```-обёртки
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
        content = content.strip()

    # разделить HTML и summary
    if SUMMARY_MARK in content:
        html_part, _, summary_part = content.partition(SUMMARY_MARK)
    else:
        html_part, summary_part = content, ""
    new_html = _dedash(html_part.strip())
    summary = _parse_summary(summary_part)

    _validate(original_html, new_html)
    return new_html, summary


def _parse_summary(raw: str) -> str:
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        return "Изменения применены."
    out = []
    for l in lines[:6]:
        out.append(l if l.startswith(("-", "•", "*")) else f"- {l}")
    return "\n".join(out)


def _validate(original: str, new_html: str) -> None:
    if len(new_html) < 200:
        raise EditError("Модель вернула слишком короткий результат.")
    # потеря контента: новый сильно короче исходного
    if len(new_html) < 0.6 * len(original):
        raise EditError(
            f"Похоже, потерян контент (было {len(original)}, стало {len(new_html)} симв).")
    # не сломана ли закрывающая структура
    for tag in ("</html>", "</article>"):
        if tag in original and tag not in new_html:
            raise EditError(f"В ответе пропал {tag} — структура нарушена.")


def apply_edit_gpt(*, slug: str, current_version: str, versions: list[str],
                   edit_text: str, timeout_sec: int = 200,
                   model: str = DEFAULT_MODEL):
    """Полный аналог bot.editor.apply_edit, но через OpenAI. Возвращает EditResult."""
    # Лениво, чтобы не было циклического импорта при загрузке bot.editor.
    from bot.editor import (EditResult, _next_version, _count_html_chars,
                            _git_publish_new_version)
    from bot.config import DRAFTS_DIR

    folder = DRAFTS_DIR / slug
    versions_dir = folder / "versions"
    versions_dir.mkdir(exist_ok=True)

    current_path = versions_dir / f"v{current_version}.html"
    if not current_path.exists():
        fallback = versions_dir / "v2.0.html"
        if not fallback.exists():
            fallback = folder / "article-v2.html"
        if not fallback.exists():
            return EditResult(False, None, None, "", None,
                              f"Не найдена текущая версия ({current_version}) и нет fallback'а.")
        current_path = fallback

    new_version = _next_version(versions)
    next_path = versions_dir / f"v{new_version}.html"

    try:
        original = current_path.read_text(encoding="utf-8")
    except OSError as e:
        return EditResult(False, None, None, "", None,
                          f"Не удалось прочитать {current_path.name}: {e}")

    try:
        new_html, summary = edit_html_gpt(original, edit_text, model=model,
                                          timeout_sec=timeout_sec)
    except EditError as e:
        return EditResult(False, None, None, "", None, str(e))
    except Exception as e:  # noqa: BLE001 — наружу отдаём понятную причину
        return EditResult(False, None, None, "", None, f"GPT-редактор упал: {e}")

    if new_html.strip() == original.strip():
        return EditResult(False, None, None, "", None,
                          "Модель не внесла изменений в файл.")

    try:
        next_path.write_text(new_html, encoding="utf-8")
    except OSError as e:
        return EditResult(False, None, None, "", None,
                          f"Не удалось записать {next_path.name}: {e}")

    char_count = _count_html_chars(next_path)

    # Git push новой версии в фоне (как в claude-версии).
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
