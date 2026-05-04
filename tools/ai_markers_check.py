"""
Регекс-чекер ИИ-маркеров по .claude/style/anti-ai-style.md.

Запуск:
    python -m tools.ai_markers_check <path>
    python -m tools.ai_markers_check drafts/spisat-dolgi-po-kreditam-bez-imushchestva/article.html
    python -m tools.ai_markers_check drafts/                    # рекурсивно по всем article.html и draft.md
    python -m tools.ai_markers_check article.html --json
    python -m tools.ai_markers_check article.html --threshold 3 # порог маркеров на 1000 знаков

Выход:
    0 - если плотность маркеров <= threshold
    1 - если превышает или путь не найден

HTML обрабатывается так: вырезается содержимое <script>, <style>, <code>, <pre>,
<blockquote> (цитаты закона). Анализируется только видимый авторский текст.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Pattern:
    name: str
    category: str
    severity: str  # critical | high | medium | low
    regex: re.Pattern
    note: str = ""


def _rx(pat: str) -> re.Pattern:
    return re.compile(pat, re.IGNORECASE | re.UNICODE)


# Паттерны сгруппированы по разделам anti-ai-style.md.
# severity:
#   critical - запрещено стайл-гайдом полностью (длинные тире, эмодзи, англ. кавычки, артефакты чатбота)
#   high     - сильные ИИ-маркеры (раздувание, рекламный язык, отрицательные параллелизмы)
#   medium   - типичная ИИ-лексика (канцелярит, обороты-паразиты)
#   low      - частотные слова, требуют контекстной проверки человеком
PATTERNS: list[Pattern] = [
    # === 1. Раздувание значимости ===
    Pattern("Раздувание: «является важным/ключевым»", "inflate", "high",
            _rx(r"\bявляется\s+(важн\w+|ключев\w+|значим\w+|основопол\w+|неотъем\w+)")),
    Pattern("Раздувание: «свидетельствует о»", "inflate", "high",
            _rx(r"\bсвидетельству\w+\s+о\b")),
    Pattern("Раздувание: «играет ключевую роль»", "inflate", "high",
            _rx(r"\bигра\w+\s+(ключев\w+|важн\w+)\s+рол\w+")),
    Pattern("Раздувание: «знаменует/оставляет след/краеугольный»", "inflate", "high",
            _rx(r"\b(знамену\w+|оставля\w+\s+неизглад\w+|краеугольн\w+)")),
    Pattern("Раздувание: «подчёркивает важность»", "inflate", "high",
            _rx(r"\bподчёрк\w+\s+важност\w+")),

    # === 2. Навязчивая авторитетность ===
    Pattern("Авторитетность: «по мнению экспертов»", "authority", "high",
            _rx(r"\bпо\s+мнен\w+\s+эксперт\w+")),
    Pattern("Авторитетность: «ведущие издания/признанный авторитет»", "authority", "high",
            _rx(r"\b(ведущ\w+\s+издан\w+|признан\w+\s+авторитет\w+)")),

    # === 3. Поверхностные деепричастия ===
    Pattern("Деепричастный оборот ИИ", "participle", "medium",
            _rx(r"(?<![А-яЁё])(подчёркива|демонстриру|свидетельству|способству|отража|символизиру|формиру)я\b"),
            "Часто пустой оборот — удалять или заменять глаголом."),

    # === 4. Рекламный язык ===
    Pattern("Реклама: «может похвастаться/ярк/самобытн»", "ad", "high",
            _rx(r"\b(может\s+похваст\w+|ярк\w+|самобытн\w+|непревзойдён\w+)")),
    Pattern("Реклама: «уникальный/неповторимый/поистине»", "ad", "high",
            _rx(r"\b(уникальн\w+|неповторим\w+|поистин\w+|по-настоящ\w+)")),
    Pattern("Реклама: «в самом сердце/живописн/захватывающий дух»", "ad", "high",
            _rx(r"\b(в\s+самом\s+сердц\w+|живописн\w+|захватыва\w+\s+дух)")),
    Pattern("Реклама: «не может не впечатлять»", "ad", "high",
            _rx(r"\bне\s+может\s+не\s+впечатл\w+")),
    Pattern("Реклама: «раскрывает потенциал/богатое наследие»", "ad", "high",
            _rx(r"\b(раскрыва\w+\s+потенциал\w*|богат\w+\s+наследи\w+)")),
    Pattern("Реклама: «самый дешёвый/самый быстрый» (без обоснования)", "ad", "high",
            _rx(r"\bсам\w+\s+(дешёв\w+|дешев\w+|быстр\w+|выгодн\w+|надёжн\w+|надежн\w+|лучш\w+)\b"),
            "Оценочное превосходство без цифр и сравнения."),

    # === 5. Слова-пустышки ===
    Pattern("Пустышка: «по данным отраслевых отчётов»", "vague", "high",
            _rx(r"\bпо\s+данн\w+\s+отрасл\w+\s+отчёт\w+")),
    Pattern("Пустышка: «наблюдатели отмечают / эксперты полагают»", "vague", "high",
            _rx(r"\b(наблюдател\w+\s+отмеча\w+|эксперт\w+\s+полага\w+|ряд\s+специалист\w+\s+счита\w+)")),
    Pattern("Пустышка: «согласно различным источникам»", "vague", "high",
            _rx(r"\bсогласно\s+различн\w+\s+источник\w+")),

    # === 7. ИИ-лексика с завышенной частотой ===
    Pattern("ИИ-лексика: «кроме того»", "ai-lexis", "medium",
            _rx(r"(?:^|[\.\!\?»\)\s])кроме\s+того[\,\.]")),
    Pattern("ИИ-лексика: «в контексте»", "ai-lexis", "medium",
            _rx(r"\bв\s+контексте\b")),
    Pattern("ИИ-лексика: «углубиться в тему»", "ai-lexis", "medium",
            _rx(r"\bуглуб\w+\s+в\s+(тем\w+|деталь\w+|вопрос\w+)")),
    Pattern("ИИ-лексика: «непреходящ/знаков»", "ai-lexis", "medium",
            _rx(r"\b(непреходящ\w+|знаков\w+\s+(событи\w+|момент\w+|роль|вклад))")),
    Pattern("ИИ-лексика: «усиливать/способствовать (абстрактно)»", "ai-lexis", "low",
            _rx(r"\b(усилива\w+|усилить|способству\w+)\b"),
            "Частотные глаголы; смотреть в контексте."),
    Pattern("ИИ-лексика: «привлекать внимание»", "ai-lexis", "medium",
            _rx(r"\bпривлека\w+\s+внимани\w+")),
    Pattern("ИИ-лексика: «взаимодействие/тонкости/нюансы (абстрактно)»", "ai-lexis", "low",
            _rx(r"\b(взаимодействи\w+|тонкост\w+|нюанс\w+)\b"),
            "Допустимо при конкретике; абстрактно — убирать."),
    Pattern("ИИ-лексика: «ландшафт/палитра (абстрактно)»", "ai-lexis", "high",
            _rx(r"\b(ландшафт\w+|палитр\w+)\b"),
            "В юр. текстах почти всегда лишние."),
    Pattern("ИИ-лексика: «продемонстрировать»", "ai-lexis", "medium",
            _rx(r"\bпродемонстрир\w+")),
    Pattern("ИИ-лексика: «акцентировать»", "ai-lexis", "medium",
            _rx(r"\bакцентир\w+")),

    # === 8. Избегание простого «есть/это» ===
    Pattern("Обёртка: «представляет собой»", "wrap", "high",
            _rx(r"\bпредставля\w+\s+собой\b")),
    Pattern("Обёртка: «служит/выступает в роли»", "wrap", "high",
            _rx(r"\b(служит\s+(чем-то|инструмент|основ|средств|механизм|способ)|выступа\w+\s+в\s+рол\w+)")),
    Pattern("Обёртка: «олицетворяет/воплощает в себе»", "wrap", "high",
            _rx(r"\b(олицетвор\w+|воплоща\w+\s+в\s+себе)")),

    # === 9. Отрицательные параллелизмы ===
    Pattern("Параллелизм: «не просто X, а Y»", "parallel", "high",
            _rx(r"\bне\s+просто\s+\S+(\s+\S+){0,5}?[,]\s*а\s+")),
    Pattern("Параллелизм: «не только X, но и Y»", "parallel", "high",
            _rx(r"\bне\s+только\s+\S+(\s+\S+){0,8}?[,]\s*но\s+и\s+")),
    Pattern("Параллелизм: «дело не в X, дело в Y»", "parallel", "high",
            _rx(r"\bдело\s+не\s+в\s+\S+(\s+\S+){0,5}?[,]\s*дело\s+в\s+")),
    Pattern("Параллелизм: «это не тупик, а Y»", "parallel", "high",
            _rx(r"\bэто\s+не\s+\S+[,]\s*а\s+\S+"),
            "Подражание форме — звучит как ИИ-афоризм."),

    # === 13. Длинные тире ===
    Pattern("Длинное тире (—)", "punct", "critical",
            _rx(r"—"),
            "По CLAUDE.md длинные тире запрещены полностью."),

    # === 16. Заголовки «Каждое Слово С Большой Буквы» — оценивается отдельно
    # === 17. Эмодзи ===
    Pattern("Эмодзи в тексте", "emoji", "critical",
            re.compile(
                r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]",
                re.UNICODE,
            )),

    # === 18. Английские кавычки ===
    Pattern("Английские кавычки в тексте", "quotes", "critical",
            _rx(r'(?<![=:\(\[\{])"[А-яЁёA-Za-z0-9]'),
            "Только «ёлочки» и „лапки“; прямые кавычки в HTML-атрибутах игнорируются."),

    # === 19. Артефакты чатбота ===
    Pattern("Чатбот: «надеюсь, это поможет»", "chatbot", "critical",
            _rx(r"\bнадеюсь[,]?\s+(это|данное)\s+помож\w+")),
    Pattern("Чатбот: «конечно!/безусловно!/вы абсолютно правы»", "chatbot", "critical",
            _rx(r"(?:^|[\.\!\?»\s])(конечно!|безусловно!|вы\s+абсолютно\s+правы)")),
    Pattern("Чатбот: «дайте знать/вот обзор»", "chatbot", "critical",
            _rx(r"\b(дайте\s+знать|вот\s+(обзор|подборк\w+|список))\b")),

    # === 20. Дисклеймеры о знаниях ===
    Pattern("Дисклеймер ИИ: «по состоянию на/насколько мне известно»", "disclaimer", "high",
            _rx(r"\b(по\s+состоянию\s+на\s+\d|насколько\s+мне\s+известн\w+|доступн\w+\s+источник\w+\s+не\s+содерж\w+)")),

    # === 21. Подхалимский тон ===
    Pattern("Подхалимаж: «отличный вопрос/прекрасное замечание»", "sycophant", "critical",
            _rx(r"\b(отличн\w+\s+вопрос|прекрасн\w+\s+замечани\w+)")),

    # === 22. Фразы-паразиты ===
    Pattern("Паразит: «для того, чтобы»", "parasite", "medium",
            _rx(r"\bдля\s+того[,]?\s+чтобы\b")),
    Pattern("Паразит: «в связи с тем, что»", "parasite", "medium",
            _rx(r"\bв\s+связи\s+с\s+тем[,]?\s+что\b")),
    Pattern("Паразит: «в настоящий момент времени»", "parasite", "medium",
            _rx(r"\bв\s+настоящ\w+\s+момент\w*\s+времен\w+")),
    Pattern("Паразит: «в случае, если»", "parasite", "medium",
            _rx(r"\bв\s+случае[,]?\s+если\b")),
    Pattern("Паразит: «обладает способностью»", "parasite", "medium",
            _rx(r"\bобладает\s+способн\w+")),
    Pattern("Паразит: «важно отметить тот факт, что»", "parasite", "high",
            _rx(r"\bважно\s+отметить\s+тот\s+факт[,]?\s+что\b")),
    Pattern("Паразит: «данный/данная/данное» (вместо «этот»)", "parasite", "low",
            _rx(r"\b(данн[ыая]\w*|данное)\s+(вопрос\w*|случа\w*|пункт\w*|положени\w*|материал\w*|раздел\w*)"),
            "В юр. текстах часто прокрадывается канцелярит «данный»."),
    Pattern("Паразит: «осуществлять»", "parasite", "medium",
            _rx(r"\bосуществл\w+")),

    # === 23. Избыточное хеджирование ===
    Pattern("Хеджирование: «можно предположить, что, возможно»", "hedge", "high",
            _rx(r"\bможно\s+предположить[,]?\s+что[,]?\s+возможн\w+")),
    Pattern("Хеджирование: «потенциально мог бы оказать определённое»", "hedge", "high",
            _rx(r"\bпотенциальн\w+\s+мог\w*\s+(бы\s+)?оказ\w+\s+определ\w+")),

    # === 24. Шаблонные позитивные концовки ===
    Pattern("Концовка: «будущее выглядит многообещающим»", "ending", "critical",
            _rx(r"\bбудущее\s+(выгляд\w+|представл\w+)\s+(многообещ\w+|перспектив\w+)")),
    Pattern("Концовка: «впереди захватывающие времена»", "ending", "critical",
            _rx(r"\bвпереди\s+захватыва\w+\s+времен\w+")),
    Pattern("Концовка: «шаг в правильном направлении»", "ending", "high",
            _rx(r"\bшаг\s+в\s+правильн\w+\s+направлен\w+")),

    # === 25. Канцелярит ===
    Pattern("Канцелярит: «в рамках» (без нужды)", "clerical", "low",
            _rx(r"\bв\s+рамках\b"),
            "Часто можно убрать без потери смысла."),
    Pattern("Канцелярит: «на данный момент»", "clerical", "medium",
            _rx(r"\bна\s+данн\w+\s+момент\w*")),
    Pattern("Канцелярит: «вышеупомянут/нижеследующ»", "clerical", "high",
            _rx(r"\b(вышеупомянут\w+|нижеследующ\w+)")),
    Pattern("Канцелярит: «надлежащий/имеет место быть»", "clerical", "high",
            _rx(r"\b(надлежащ\w+|имеет\s+место\s+быть)")),
    Pattern("Канцелярит: «в целях»", "clerical", "medium",
            _rx(r"\bв\s+цел\w+\s+\S+ия\b"),
            "Заменять на «чтобы» / «для»."),
    Pattern("Канцелярит: «в соответствии с» (вне цитаты закона)", "clerical", "low",
            _rx(r"\bв\s+соответствии\s+с\b"),
            "В цитате закона — норма; в авторском — заменять на «по»."),

    # === 26. Избыточные вводные ===
    Pattern("Вводное: «стоит отметить, что»", "intro", "high",
            _rx(r"\bстоит\s+отметить[,]?\s+что\b")),
    Pattern("Вводное: «необходимо подчеркнуть, что»", "intro", "high",
            _rx(r"\bнеобходимо\s+подчеркн\w+[,]?\s+что\b")),
    Pattern("Вводное: «важно учитывать тот факт»", "intro", "high",
            _rx(r"\bважно\s+учитыв\w+\s+тот\s+факт\b")),
    Pattern("Вводное: «нельзя не обратить внимание»", "intro", "high",
            _rx(r"\bнельзя\s+не\s+обрат\w+\s+внимани\w+")),
    Pattern("Вводное: «не менее важным является»", "intro", "high",
            _rx(r"\bне\s+менее\s+важн\w+\s+являет\w+")),
    Pattern("Вводное: «следует обратить внимание»", "intro", "medium",
            _rx(r"\bследует\s+обрат\w+\s+внимани\w+")),

    # === 27. «Мир/сфера/область» как обёртка ===
    Pattern("Обёртка: «в мире/в сфере/в области» + абстракция", "scope-wrap", "high",
            _rx(r"\bв\s+(мире|сфере|области)\s+(банкротств\w+|юриспруденц\w+|взыскан\w+|финанс\w+|долг\w+)")),

    # === Доп. наблюдения от агента-критика (Баден-Баден риски) ===
    Pattern("Маркер: «как раз тот случай»", "extra", "high",
            _rx(r"\bкак\s+раз\s+тот\s+случай\b")),
    Pattern("Маркер: «по логике закона»", "extra", "medium",
            _rx(r"\bпо\s+логике\s+закон\w+")),
    Pattern("Маркер: «констатирует/лишь констатирует»", "extra", "medium",
            _rx(r"\b(лишь\s+)?констатир\w+")),
    Pattern("Маркер: «звонки никуда не деваются»", "extra", "medium",
            _rx(r"\bзвонки\s+никуда\s+не\s+дева\w+")),

    # === Writer B anti-AI (май 2026) ===
    # Сравнение двух статей (37% AI и 0% AI) показало: text.ru различает
    # их в основном по перплексии n-граммов, не по структурным паттернам
    # (многие наши «маркеры ChatGPT» встречаются в обеих).
    #
    # Поэтому оставлены только те паттерны, что СПЕЦИФИЧНЫ для Writer B
    # и НЕ встречаются у эталона. Это не панацея — реальное снижение AI
    # требует forced few-shot эталона. Но эти маркеры закроют самые явные
    # ChatGPT-структуры.

    Pattern("Writer B: подзаголовок-якорь «Срок./Стоимость./Условия.»", "writer-b", "high",
            _rx(r"(?:^|[\.\!\?»\)\n]\s+)(Срок|Стоимост\w+|Услови\w+|"
                r"Преимуществ\w+|Недостатк\w+|Главный\s+риск)\.\s+[А-ЯЁ]"),
            "ChatGPT-структура «слово-якорь + точка + предложение». "
            "Заменять на связный текст: «Подходит, когда денег хватает на всех» "
            "вместо «Условия. Подходит, когда…»"),

    Pattern("Writer B: «Дальше разберём / Далее рассмотрим»", "writer-b", "high",
            _rx(r"\b(Дальше|Далее|Ниже)\s+(разбер\w+|посмотр\w+|рассмотр\w+|"
                r"расскаж\w+|опиш\w+)"),
            "Вступительные обороты-указатели — типичный ChatGPT. "
            "Просто переходим к следующему блоку без анонса."),

    Pattern("Writer B: канцелярские пары глаголов «X-ется и Y-ется»", "writer-b", "high",
            _rx(r"\b(фиксиру\w+|оформля\w+|рассматрива\w+|"
                r"осуществля\w+|регистриру\w+|производ\w+)ся\s+и\s+"
                r"\w+(?:ется|ются|уется|ируется)\b"),
            "Канцелярские пассивные пары. «Фиксируется и квалифицируется» → "
            "«налоговая видит и трактует»"),

    Pattern("Writer B: вступление «Если вы дошли/задумались»", "writer-b", "high",
            _rx(r"\bЕсли\s+вы\s+(дошли\s+до\s+вопрос\w+|задумалис\w+|"
                r"читаете\s+эту\s+стать\w+|оказались\s+в)"),
            "Шаблонное обращение к читателю в начале статьи. "
            "Заменять на конкретный лид с фактом."),

    Pattern("Writer B: «правильный вопрос звучит не X»", "writer-b", "high",
            _rx(r"\bправильн\w+\s+вопрос\s+(звуч\w+\s+)?не\s+\S")),

    Pattern("Writer B: «закон даёт N вариантов/сценариев»", "writer-b", "medium",
            _rx(r"\bзакон\s+(даёт|даст|предусматрива\w+|закрепля\w+|"
                r"устанавлива\w+)\s+(\w+\s+){0,3}?(вариант|сценари|спосо|пут)"),
            "Канцелярское вступление к перечислению. "
            "Заменять на прямое: «вариантов три»"),
]


@dataclass
class Hit:
    pattern: Pattern
    match: str
    context: str
    position: int


@dataclass
class Report:
    file: str
    text_chars: int
    hits: list[Hit] = field(default_factory=list)
    own_voice_hits: int = 0  # «мы считаем», «по нашему опыту», «в нашей практике», «мы видим»
    first_person_singular_hits: int = 0  # «я считаю», «по моему опыту» - запрещены, должно быть 0

    @property
    def density_per_1000(self) -> float:
        if self.text_chars == 0:
            return 0.0
        return round(len(self.hits) / self.text_chars * 1000, 2)

    @property
    def by_severity(self) -> dict[str, int]:
        out = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for h in self.hits:
            out[h.pattern.severity] += 1
        return out


OWN_VOICE_RX = _rx(
    r"\b("
    r"мы\s+счита\w+|мы\s+дума\w+|"
    r"по\s+нашей\s+практик\w*|на\s+нашей\s+практик\w*|по\s+нашему\s+опыт\w*|на\s+нашем\s+опыт\w*|"
    r"в\s+нашей\s+практик\w*|на\s+нашем\s+опыт\w*|"
    r"мы\s+видим|мы\s+часто\s+видим|мы\s+видели|мы\s+часто\s+встреча\w+|мы\s+встреча\w+|мы\s+работа\w+\s+с"
    r")"
)

# Запрещённое первое лицо единственного числа в авторских конструкциях.
# Ловим, чтобы перевести на «мы».
FIRST_PERSON_SINGULAR_RX = _rx(
    r"\b("
    r"я\s+счита\w+|я\s+дума\w+|я\s+вижу|я\s+видел|я\s+работа\w+|я\s+полага\w+|"
    r"по\s+моему\s+опыт\w*|на\s+моём\s+опыт\w*|в\s+моей\s+практик\w*|"
    r"мой\s+опыт|моя\s+практик\w*"
    r")"
)


def extract_text_from_html(html: str) -> str:
    # Убираем служебные блоки и цитаты закона (blockquote часто содержит цитаты).
    cleaned = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style\b[^>]*>.*?</style>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<code\b[^>]*>.*?</code>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<pre\b[^>]*>.*?</pre>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<blockquote\b[^>]*>.*?</blockquote>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<head\b[^>]*>.*?</head>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"&nbsp;", " ", cleaned)
    cleaned = re.sub(r"&[a-zA-Z]+;", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def extract_text_from_markdown(md: str) -> str:
    # Убираем blockquotes (>), кодовые блоки и инлайн-код.
    lines = [l for l in md.splitlines() if not l.lstrip().startswith(">")]
    text = "\n".join(lines)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]+`", " ", text)
    return text


def context_of(text: str, start: int, end: int, window: int = 60) -> str:
    a = max(0, start - window)
    b = min(len(text), end + window)
    snippet = text[a:b].replace("\n", " ")
    return ("..." if a > 0 else "") + snippet + ("..." if b < len(text) else "")


def analyze(file_path: Path) -> Report:
    raw = file_path.read_text(encoding="utf-8")
    if file_path.suffix.lower() in {".html", ".htm"}:
        text = extract_text_from_html(raw)
    elif file_path.suffix.lower() in {".md", ".markdown"}:
        text = extract_text_from_markdown(raw)
    else:
        text = raw

    rep = Report(file=str(file_path.relative_to(PROJECT_ROOT) if file_path.is_relative_to(PROJECT_ROOT) else file_path),
                 text_chars=len(text))

    for pat in PATTERNS:
        for m in pat.regex.finditer(text):
            rep.hits.append(Hit(
                pattern=pat,
                match=m.group(0),
                context=context_of(text, m.start(), m.end()),
                position=m.start(),
            ))

    rep.own_voice_hits = len(OWN_VOICE_RX.findall(text))
    rep.first_person_singular_hits = len(FIRST_PERSON_SINGULAR_RX.findall(text))
    return rep


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def print_report(rep: Report, verbose: bool = True) -> None:
    print(f"\n=== {rep.file} ===")
    print(f"Текст: {rep.text_chars:,} знаков")
    print(f"Маркеров найдено: {len(rep.hits)}  (плотность {rep.density_per_1000} на 1000 знаков)")
    sev = rep.by_severity
    print(f"  critical: {sev['critical']}  high: {sev['high']}  medium: {sev['medium']}  low: {sev['low']}")
    print(f"«Голос компании» (мы считаем / по нашему опыту / в нашей практике): {rep.own_voice_hits}")
    if rep.first_person_singular_hits > 0:
        print(f"  ⚠  Запрещённое первое лицо «я» (я считаю / по моему опыту): {rep.first_person_singular_hits} - должно быть 0")

    if not verbose or not rep.hits:
        return

    by_pat: dict[str, list[Hit]] = {}
    for h in rep.hits:
        by_pat.setdefault(h.pattern.name, []).append(h)

    ordered = sorted(
        by_pat.items(),
        key=lambda kv: (SEVERITY_ORDER[kv[1][0].pattern.severity], -len(kv[1]), kv[0]),
    )

    print("\nДетали:")
    for name, hits in ordered:
        p = hits[0].pattern
        print(f"\n  [{p.severity.upper()}] {name}  x{len(hits)}")
        if p.note:
            print(f"      ! {p.note}")
        for h in hits[:3]:
            print(f"      «{h.match}»  →  {h.context}")
        if len(hits) > 3:
            print(f"      ... ещё {len(hits) - 3}")


def to_dict(rep: Report) -> dict:
    return {
        "file": rep.file,
        "text_chars": rep.text_chars,
        "hits_total": len(rep.hits),
        "density_per_1000": rep.density_per_1000,
        "by_severity": rep.by_severity,
        "own_voice_hits": rep.own_voice_hits,
        "first_person_singular_hits": rep.first_person_singular_hits,
        "hits": [
            {
                "name": h.pattern.name,
                "category": h.pattern.category,
                "severity": h.pattern.severity,
                "match": h.match,
                "position": h.position,
                "context": h.context,
            }
            for h in rep.hits
        ],
    }


def collect_targets(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        files: list[Path] = []
        for ext in ("*.html", "*.md"):
            files.extend(path.rglob(ext))
        # Исключаем очевидно служебные.
        exclude = {"README.md", "CLAUDE.md", "BACKEND.md"}
        return sorted(f for f in files if f.name not in exclude and "node_modules" not in f.parts)
    raise FileNotFoundError(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Регекс-чекер ИИ-маркеров по anti-ai-style.md")
    parser.add_argument("path", help="Путь к файлу (.html/.md) или директории")
    parser.add_argument("--threshold", type=float, default=2.0,
                        help="Максимум маркеров на 1000 знаков (по умолчанию 2.0)")
    parser.add_argument("--json", action="store_true", help="Вывод в JSON")
    parser.add_argument("--quiet", action="store_true", help="Только итог по файлам")
    args = parser.parse_args()

    target = Path(args.path).resolve()
    if not target.exists():
        print(f"Не найдено: {target}", file=sys.stderr)
        return 1

    files = collect_targets(target)
    if not files:
        print(f"В {target} не найдено .html/.md файлов", file=sys.stderr)
        return 1

    reports = [analyze(f) for f in files]

    if args.json:
        print(json.dumps([to_dict(r) for r in reports], ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            print_report(rep, verbose=not args.quiet)

        if len(reports) > 1:
            print("\n=== Сводка ===")
            for rep in sorted(reports, key=lambda r: -r.density_per_1000):
                sev = rep.by_severity
                print(f"  {rep.density_per_1000:>5.2f}/1k  hits={len(rep.hits):>3}  "
                      f"crit={sev['critical']}  high={sev['high']}  voice={rep.own_voice_hits}  "
                      f"я={rep.first_person_singular_hits}  {rep.file}")

    over = [r for r in reports if r.density_per_1000 > args.threshold or r.first_person_singular_hits > 0]
    if over:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
