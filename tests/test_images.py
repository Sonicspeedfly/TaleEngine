"""
2.17.0: аватарки и фоны — сжатыми копиями по ссылке, исходник хранится и
скачивается. Списки больше не таскают мегабайты фото.
"""
import base64
import io

from PIL import Image


def _char(client, name, avatar, headers=None):
    cid = client.post("/api/characters", json={"name": name}, headers=headers).json()["id"]
    client.patch(f"/api/characters/{cid}", json={"avatar_path": avatar}, headers=headers)
    return cid


def _photo(w=1600, h=2000, color=(200, 120, 40)) -> str:
    im = Image.effect_noise((w, h), 40).convert("RGB")
    im = Image.blend(im, Image.new("RGB", (w, h), color), 0.5)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def test_character_avatar_goes_as_small_cached_copy(client):
    src = _photo()
    cid = _char(client, "Фото", src)
    chars = client.get("/api/characters").json()
    ava = next(c for c in chars if c["id"] == cid)["avatar_path"]
    assert ava.startswith(f"/api/img/character/{cid}/thumb?v=")
    r = client.get(ava)
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]
    im = Image.open(io.BytesIO(r.content))
    assert max(im.size) <= 256 and len(r.content) < len(src) / 20
    # Оригинал — тот же файл, что загружали.
    orig = client.get(f"/api/img/character/{cid}/orig?name=Фото")
    assert orig.content == base64.b64decode(src.split(",", 1)[1])
    assert orig.headers["content-disposition"].startswith("attachment")
    # Клиент сохраняет карточку и шлёт ссылку обратно — исходник не затирается.
    client.patch(f"/api/characters/{cid}", json={"description": "новое", "avatar_path": ava})
    assert client.get(f"/api/img/character/{cid}/orig").content == orig.content


def test_new_avatar_changes_version(client):
    cid = _char(client, "Смена", _photo(color=(10, 10, 200)))
    first = client.get("/api/characters").json()
    v1 = next(c for c in first if c["id"] == cid)["avatar_path"]
    out = client.patch(f"/api/characters/{cid}", json={"avatar_path": _photo(color=(200, 10, 10))}).json()
    assert out["avatar_path"] != v1 and out["avatar_path"].startswith("/api/img/")
    # Старая версия по старому адресу больше не «навсегда».
    assert "immutable" not in client.get(v1).headers["cache-control"]
    client.patch(f"/api/characters/{cid}", json={"avatar_path": ""})
    assert not next(c for c in client.get("/api/characters").json() if c["id"] == cid)["avatar_path"]


def test_session_background_is_link_in_list(client):
    cid = client.post("/api/characters", json={"name": "Фон"}).json()["id"]
    sid = client.post(f"/api/sessions?character_id={cid}").json()["session_id"]
    r = client.patch(f"/api/sessions/{sid}", json={"background": _photo(2400, 1400)})
    bg = r.json()["background"]
    assert bg.startswith(f"/api/img/session_bg/{sid}/view?v=")
    row = next(s for s in client.get("/api/sessions").json() if s["id"] == sid)
    assert row["background"] == bg
    im = Image.open(io.BytesIO(client.get(bg).content))
    assert max(im.size) <= 1920
    # CSS-градиент — не картинка: остаётся как есть.
    client.patch(f"/api/sessions/{sid}", json={"background": "linear-gradient(#000, #333)"})
    row = next(s for s in client.get("/api/sessions").json() if s["id"] == sid)
    assert row["background"] == "linear-gradient(#000, #333)"


def test_persona_avatar_link_and_unknown_kind(client):
    p = client.post("/api/personas", json={"name": "Я", "avatar_path": _photo(900, 900)}).json()
    assert p["avatar_path"].startswith("/api/img/persona/")
    listed = next(x for x in client.get("/api/personas").json() if x["id"] == p["id"])
    assert listed["avatar_path"] == p["avatar_path"]
    assert client.get("/api/img/nope/1/thumb").status_code == 404
    assert client.get(f"/api/img/persona/{p['id']}/view").status_code == 404


def test_image_of_foreign_character_is_hidden(client):
    from backend import admin_service

    # Режим аккаунтов: персонаж одного пользователя не виден другому — и его аватар тоже.
    cache = admin_service.security_cache()
    old = dict(cache)
    cache["accounts_enabled"] = True
    try:
        a = client.post("/api/auth/register", json={"username": "img_a", "password": "pass12345"}).json()
        b = client.post("/api/auth/register", json={"username": "img_b", "password": "pass12345"}).json()
        if not (a.get("token") and b.get("token")):
            return   # регистрация закрыта в этой конфигурации тестов
        ha, hb = {"X-User-Token": a["token"]}, {"X-User-Token": b["token"]}
        cid = _char(client, "Секрет", _photo(600, 600), headers=ha)
        assert client.get(f"/api/img/character/{cid}/thumb", headers=ha).status_code == 200
        assert client.get(f"/api/img/character/{cid}/thumb", headers=hb).status_code == 404
        assert client.get(f"/api/img/character/{cid}/orig", headers=hb).status_code == 404
    finally:
        cache.clear()
        cache.update(old)


def test_broken_and_unsupported_images_do_not_break_saves(client):
    from backend import images

    cid = client.post("/api/characters", json={"name": "Битый"}).json()["id"]
    # Не base64 и битый base64 — сохранение проходит, отдаётся исходник.
    for bad in ("data:image/png;base64,abc", "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'/>"):
        r = client.patch(f"/api/characters/{cid}", json={"avatar_path": bad})
        assert r.status_code == 200, r.text
        assert r.json()["avatar_path"] == bad
    # SVG не сжимается: версия помечена, повторных попыток нет.
    spawned = []
    orig = images._spawn_ensure
    images._spawn_ensure = lambda kind, oid: spawned.append((kind, oid))
    try:
        listed = next(c for c in client.get("/api/characters").json() if c["id"] == cid)
    finally:
        images._spawn_ensure = orig
    assert listed["avatar_path"].startswith("data:image/svg") and not spawned
    # Отдача по ссылке — исходник в песочнице (SVG не исполнит скрипт).
    r = client.get(f"/api/img/character/{cid}/thumb")
    assert r.status_code == 200 and r.headers["content-security-policy"] == "sandbox"


def test_multi_frame_phone_jpeg_is_compressed():
    from backend import images

    big = Image.effect_noise((3000, 2000), 50).convert("RGB")
    depth = Image.new("RGB", (600, 400), (128, 128, 128))
    buf = io.BytesIO()
    big.save(buf, "MPO", save_all=True, append_images=[depth], quality=90)
    value = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    data, mime, w, h = images.render(value, 256)
    assert mime == "image/webp" and max(w, h) <= 256 and len(data) < len(buf.getvalue()) / 20


def test_deleted_user_avatar_copy_is_dropped(client):

    from backend import images, models
    from backend.database import AsyncSessionLocal
    from sqlalchemy import select

    async def scenario():
        async with AsyncSessionLocal() as db:
            u = models.User(username="img_gone", password_hash="x", avatar_path=_photo(400, 400))
            db.add(u)
            await db.commit()
            uid = u.id
            assert await images.ensure(db, "user", uid)
        return uid

    uid = client.portal.call(scenario)
    client.delete(f"/api/admin/users/{uid}")

    async def left():
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(models.ImageVariant).where(
                models.ImageVariant.kind == "user", models.ImageVariant.owner_id == uid))).first()

    assert client.portal.call(left) is None
