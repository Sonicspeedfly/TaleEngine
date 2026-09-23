"""
Локальные копии CDN-библиотек (frontend/vendor/).

Там, где jsdelivr медленный или недоступен, страница без них открывалась пустой.
Раньше CDN меняли на /vendor/ правкой index.html и app.js прямо на сервере, и
каждый `git pull` упирался в эти правки. Теперь подмену делает сервер.
"""
import shutil
from pathlib import Path

import pytest

_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@pytest.fixture
def frontend_copy(tmp_path, monkeypatch):
    """Копия фронтенда во временной папке — настоящую frontend/ не трогаем."""
    from backend import main

    for name in ("index.html", "app.js", "styles.css"):
        shutil.copy(_FRONTEND / name, tmp_path / name)
    monkeypatch.setattr(main, "_frontend_dir", tmp_path)
    return tmp_path


def test_without_vendor_dir_everything_stays_on_cdn(client, frontend_copy):
    html = client.get("/").text
    assert "https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.prod.js" in html
    assert 'name="tale-vendor"' not in html
    assert "/vendor/" not in html


def test_vendor_copies_replace_only_their_own_cdn_urls(client, frontend_copy):
    vendor = frontend_copy / "vendor"
    vendor.mkdir()
    (vendor / "vue.global.prod.js").write_text("/* vue */", encoding="utf-8")
    (vendor / "katex.min.js").write_text("/* katex */", encoding="utf-8")

    html = client.get("/").text
    assert 'src="/vendor/vue.global.prod.js"' in html
    # Чего нет в vendor/, по-прежнему идёт с CDN, а не пропадает.
    assert "https://cdn.jsdelivr.net/npm/markdown-it@14/dist/markdown-it.min.js" in html
    # Список копий — для того, что app.js грузит сам по надобности (KaTeX).
    assert '<meta name="tale-vendor" content="katex.min.js,vue.global.prod.js" />' in html
    # Версии своих ассетов подставляются как и раньше.
    assert '/app.js?v=' in html and '/styles.css?v=' in html


def test_localize_cdn_is_noop_without_copies():
    from backend.main import _localize_cdn

    page = '<head><script src="https://cdn.jsdelivr.net/npm/x@1/dist/x.js"></script></head>'
    assert _localize_cdn(page, set()) == page
    assert "/vendor/x.js" in _localize_cdn(page, {"x.js"})
