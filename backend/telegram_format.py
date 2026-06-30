"""
Форматирование ответов нейросети для Telegram.

Две задачи:
  1) РАЗБИВКА длинных ответов — в Telegram одно сообщение максимум 4096 символов.
     Режем умно: по границам абзацев, потом строк, потом (в крайнем случае) жёстко;
     код-блоки ``` не рвём — если блок попал на стык, закрываем и открываем заново.
  2) СТИЛИЗАЦИЯ — нейросеть отвечает в Markdown, а Telegram его сырым не покажет
     (увидите **звёздочки**). Конвертируем Markdown в безопасный Telegram-HTML
     (<b>/<i>/<s>/<code>/<pre>/<a>/<blockquote>) — так ответ выглядит одинаково
     аккуратно и в приложении, и в Telegram.

Всё устойчиво к кривой разметке: если HTML не распарсится, вызывающая сторона
шлёт обычный текст (см. telegram_runtime.send_long).
"""
import html
import re

TG_LIMIT = 4096


# ----------------------------- Разбивка -----------------------------
def _hard_wrap(s: str, limit: int) -> list[str]:
    """Жёсткая нарезка очень длинной строки без удобных границ."""
    return [s[i:i + limit] for i in range(0, len(s), limit)]


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Разбить текст на части не длиннее limit по «мягким» границам."""
    text = (text or "").strip("\n")
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    cur = ""

    def flush():
        nonlocal cur
        if cur.strip():
            chunks.append(cur.rstrip("\n"))
        cur = ""

    for para in text.split("\n\n"):
        block = para + "\n\n"
        if len(cur) + len(block) <= limit:
            cur += block
            continue
        flush()
        if len(block) <= limit:
            cur = block
            continue
        # Абзац сам длиннее лимита — режем по строкам.
        for line in para.split("\n"):
            piece = line + "\n"
            if len(cur) + len(piece) <= limit:
                cur += piece
                continue
            flush()
            if len(piece) <= limit:
                cur = piece
            else:
                parts = _hard_wrap(line, limit)
                for p in parts[:-1]:
                    chunks.append(p)
                cur = parts[-1] + "\n"
        cur += "\n"
    flush()
    return _balance_code_fences(chunks)


def _balance_code_fences(chunks: list[str]) -> list[str]:
    """Если код-блок ``` разорван между частями — закрываем и открываем заново."""
    out: list[str] = []
    reopen = False
    for ch in chunks:
        if reopen:
            ch = "```\n" + ch
            reopen = False
        if ch.count("```") % 2 == 1:  # нечётное число ``` -> блок открыт
            ch = ch + "\n```"
            reopen = True
        out.append(ch)
    return out


# --------------------------- Markdown -> HTML ---------------------------
def markdown_to_html(text: str) -> str:
    """
    Конвертирует основной Markdown в Telegram-HTML. Поддержано: код-блоки,
    инлайн-код, жирный, курсив, зачёркнутый, ссылки, заголовки (как жирный),
    цитаты, маркеры списков. Остальное отдаём как текст.
    """
    if not text:
        return ""

    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    # 1) Блоки кода ```lang\n...``` — целиком прячем (внутри ничего не форматируем).
    def block_repl(m: re.Match) -> str:
        return keep("<pre><code>" + html.escape(m.group(2)) + "</code></pre>")

    text = re.sub(r"```(\w*)\n?(.*?)```", block_repl, text, flags=re.DOTALL)

    # 2) Инлайн-код `...`
    text = re.sub(r"`([^`\n]+)`", lambda m: keep("<code>" + html.escape(m.group(1)) + "</code>"), text)

    # 3) Экранируем весь остальной текст (символы *, _, ~ остаются — они нужны ниже).
    text = html.escape(text)

    # 4) Ссылки [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        text,
    )

    # 5) Жирный / курсив / зачёркнутый.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![\*\w])\*([^*\n]+?)\*(?![\*\w])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![_\w])_([^_\n]+?)_(?![_\w])", r"<i>\1</i>", text)

    # 6) Заголовки / цитаты / маркеры списков — построчно.
    lines: list[str] = []
    for line in text.split("\n"):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if heading:
            lines.append("<b>" + heading.group(1).strip() + "</b>")
            continue
        quote = re.match(r"^\s{0,3}&gt;\s?(.*)$", line)  # '>' стал '&gt;' после escape
        if quote:
            lines.append("<blockquote>" + quote.group(1) + "</blockquote>")
            continue
        bullet = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if bullet:
            lines.append(bullet.group(1) + "• " + bullet.group(2))
            continue
        lines.append(line)
    text = "\n".join(lines)

    # 7) Возвращаем спрятанные код-фрагменты.
    text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    return text


def render_for_telegram(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Готовые к отправке HTML-куски: сперва режем Markdown, потом каждый в HTML."""
    # Видимый текст после HTML короче исходного Markdown, поэтому режем по limit
    # с небольшим запасом — гарантированно влезаем в 4096.
    parts = split_message(text, limit=min(limit, TG_LIMIT - 96))
    return [markdown_to_html(p) for p in parts] or [""]
