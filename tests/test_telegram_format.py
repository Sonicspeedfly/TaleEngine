"""
Тесты форматирования ответов для Telegram: умная разбивка длинных сообщений и
конвертация Markdown -> Telegram-HTML.
"""
from backend.telegram_format import markdown_to_html, render_for_telegram, split_message


def test_short_text_one_chunk():
    assert split_message("привет") == ["привет"]
    assert split_message("") == []


def test_long_text_split_under_limit():
    text = ("Это абзац. " * 50 + "\n\n") * 30  # заведомо больше 4096
    parts = split_message(text, limit=4096)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)


def test_split_keeps_paragraph_boundaries():
    text = "А" * 3000 + "\n\n" + "Б" * 3000
    parts = split_message(text, limit=4096)
    assert len(parts) == 2
    assert set(parts[0]) == {"А"} and set(parts[1]) == {"Б"}


def test_code_fences_balanced_across_split():
    text = "вступление\n\n```python\n" + ("x = 1\n" * 1200) + "```\n\nконец"
    parts = split_message(text, limit=4096)
    assert len(parts) > 1
    # В каждой части число ``` чётное — блок кода не оборван.
    assert all(p.count("```") % 2 == 0 for p in parts)


def test_markdown_bold_italic_code():
    html = markdown_to_html("**жирный** и *курсив* и `код`")
    assert "<b>жирный</b>" in html
    assert "<i>курсив</i>" in html
    assert "<code>код</code>" in html


def test_markdown_escapes_html_specials():
    html = markdown_to_html("если a < b и x & y")
    assert "&lt;" in html and "&amp;" in html
    assert "<b>" not in html  # никаких случайных тегов


def test_markdown_link_heading_quote():
    html = markdown_to_html("# Заголовок\n\n[ссылка](https://example.com)\n> цитата")
    assert "<b>Заголовок</b>" in html
    assert '<a href="https://example.com">ссылка</a>' in html
    assert "<blockquote>цитата</blockquote>" in html


def test_code_block_content_escaped_and_preserved():
    html = markdown_to_html("```\nif a < b: pass\n```")
    assert "<pre><code>" in html and "&lt;" in html
    # Внутри код-блока разметка НЕ применяется.
    assert markdown_to_html("```\n**not bold**\n```").count("<b>") == 0


def test_render_for_telegram_returns_html_chunks():
    chunks = render_for_telegram("**hi**\n\n" + "длинно " * 2000)
    assert len(chunks) >= 1
    assert "<b>hi</b>" in chunks[0]
