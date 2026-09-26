"""
Ядро Horae State Engine (backend/horae_state.py), настройки (horae_settings)
и промпты (horae_prompts): разбор тегов, повтор состояния с журналом правок,
хронология со свёртками, рендер блока состояния. Без БД и сети.
"""
from backend import horae_prompts as hp
from backend import horae_settings as hset
from backend import horae_state as hs

REPLY = """Марина протёрла стойку и молча подвинула Вольфу кружку.

<horae>
time:2026/2/4 15:00
location:Таверна·зал
atmosphere:шумно, пахнет элем
characters:Вольф, Марина
costume:Вольф=кожаная куртка
costume:Марина=серый фартук
item!:🗝️Ключ от подвала|ржавый, тяжёлый=Марина@карман фартука
item:🍾Старый квас(3 бутылки)|кисловатый=Вольф@стол у окна
npc:Вольф|серая шерсть/зелёные глаза=молчалив@постоянный гость {{user}}~gender:мужской~age:35~job:наёмник
affection:Вольф=40
agenda:2026/2/4|Марина обещала показать подвал(2026/2/5 20:00)
</horae>
<horaeevent>
event:important|Марина передала Вольфу ключ от подвала и попросила прийти завтра вечером
</horaeevent>"""


def _meta(text=REPLY):
    clean, meta = hs.parse_reply(text, hs.ParseContext(user_name="Лея"))
    return clean, meta


# ----------------------------------------------------------------- разбор тегов
def test_parse_strips_tags_and_reads_all_fields():
    clean, meta = _meta()
    assert clean == "Марина протёрла стойку и молча подвинула Вольфу кружку."
    assert "<horae" not in clean
    assert meta["time"] == {"date": "2026/2/4", "time": "15:00"}
    assert meta["scene"]["location"] == "Таверна·зал"
    assert meta["scene"]["characters"] == ["Вольф", "Марина"]
    assert meta["costumes"] == {"Вольф": "кожаная куртка", "Марина": "серый фартук"}
    key = meta["items"]["Ключ от подвала"]
    # U+FE0F после эмодзи не прилипает к имени (у плагина прилипал).
    assert key["icon"] == "🗝" and key["importance"] == "!" and key["holder"] == "Марина"
    assert key["location"] == "карман фартука" and key["description"] == "ржавый, тяжёлый"
    assert meta["items"]["Старый квас(3 бутылки)"]["holder"] == "Вольф"
    npc = meta["npcs"]["Вольф"]
    assert npc["appearance"] == "серая шерсть/зелёные глаза"
    assert npc["personality"] == "молчалив" and npc["relationship"] == "постоянный гость {{user}}"
    assert npc["gender"] == "мужской" and npc["age"] == "35" and npc["job"] == "наёмник"
    assert meta["affection"] == {"Вольф": {"mode": "set", "value": 40.0}}
    assert meta["agenda"][0]["date"] == "2026/2/4"
    assert meta["events"] == [{"level": "important",
                               "text": "Марина передала Вольфу ключ от подвала и попросила прийти завтра вечером"}]
    assert meta["source"] == "tags" and "<horae>" in meta["raw"]


def test_parse_ignores_tags_inside_think():
    text = "<think>Надо написать <horae>time:1/1</horae></think>Ответ.\n<horae>\nlocation:Лес\n</horae>"
    clean, meta = hs.parse_reply(text)
    assert meta["scene"]["location"] == "Лес"
    assert meta["time"]["date"] == ""
    # Рассуждение осталось нетронутым, служебный блок вне него — вырезан.
    assert "<think>Надо написать <horae>time:1/1</horae></think>" in clean
    assert "Лес" not in clean


def test_event_levels_accept_russian_and_english():
    text = ("<horaeevent>\nevent:ключевое|a\nevent:важное|b\nevent:key|c\nevent:critical|d\n"
            "event:normal|e\nevent:без уровня\n</horaeevent>")
    _, meta = hs.parse_reply(text)
    assert [e["level"] for e in meta["events"]] == [
        "critical", "important", "critical", "critical", "normal", "normal"]


def test_affection_absolute_and_relative():
    _, meta = hs.parse_reply("<horae>\naffection:Анна-Мария+5\naffection:Бор=-12.5\n</horae>")
    assert meta["affection"]["Анна-Мария"] == {"mode": "add", "value": 5.0}
    assert meta["affection"]["Бор"] == {"mode": "set", "value": -12.5}


def test_npc_partial_updates():
    _, meta = hs.parse_reply("<horae>\nnpc:Вольф|=@возлюбленный {{user}}\nnpc:Марина|~профессия:трактирщица\n</horae>")
    assert meta["npcs"]["Вольф"] == {"relationship": "возлюбленный {{user}}"}
    assert meta["npcs"]["Марина"] == {"job": "трактирщица"}


def test_agenda_done_marker_and_removal():
    _, meta = hs.parse_reply("<horae>\nagenda:2026/1/1|Сходить в подвал(выполнено)\nagenda-:пирог\n</horae>")
    assert meta["agenda"] == []
    assert meta["agenda_done"] == ["Сходить в подвал", "пирог"]


def test_relationship_and_scene_desc_pairs():
    text = ("<horae>\nlocation:Таверна\nscene_desc:двухэтажное здание у дороги\nlocation:Таверна·зал\n"
            "scene_desc:длинная стойка\nrel:Вольф>Марина=тайная симпатия|не признался\n</horae>")
    _, meta = hs.parse_reply(text)
    assert meta["scene"]["desc"] == [
        {"location": "Таверна", "desc": "двухэтажное здание у дороги"},
        {"location": "Таверна·зал", "desc": "длинная стойка"},
    ]
    assert meta["relationships"] == [{"from": "Вольф", "to": "Марина", "type": "тайная симпатия",
                                      "note": "не признался"}]


def test_loose_tail_is_parsed_and_stripped_but_prose_is_not():
    clean, meta = hs.parse_reply("Они вошли.\n\ntime: 2026/3/1 10:00\nlocation: Порт\nevent: normal|Прибыли в порт")
    assert clean == "Они вошли."
    assert meta["source"] == "loose" and meta["scene"]["location"] == "Порт"
    # Одна строка «Location:» в прозе — не данные Horae.
    clean, meta = hs.parse_reply("Он сказал:\nLocation: неизвестно, спроси у стража.")
    assert meta is None and "Location" in clean


def test_no_tags_returns_none():
    clean, meta = hs.parse_reply("Просто ответ без тегов.")
    assert meta is None and clean == "Просто ответ без тегов."


def test_strip_partial_open_block():
    assert hs.strip_tags_text("Текст\n<horae>\ntime:2026", partial=True) == "Текст"
    assert hs.strip_tags_text("Текст <horaetable:Квесты>1,1:a</horaetable> конец") == "Текст  конец"


def test_merge_meta_for_continue():
    base = {**hs.empty_meta(), "time": {"date": "2026/1/1", "time": ""},
            "events": [{"level": "normal", "text": "старое"}], "items": {"Меч": {"holder": "А"}}}
    _, new = hs.parse_reply("<horae>\ntime:2026/1/2 10:00\nitem:Щит=Б@спина\n</horae>"
                            "<horaeevent>\nevent:normal|новое\n</horaeevent>")
    merged = hs.merge_meta(base, new)
    assert merged["time"] == {"date": "2026/1/2", "time": "10:00"}
    assert set(merged["items"]) == {"Меч", "Щит"}
    assert merged["events"] == [{"level": "normal", "text": "новое"}]


# ----------------------------------------------------------------- повтор
def _entries(*metas):
    return [(i + 1, "assistant", m, False) for i, m in enumerate(metas)]


def test_replay_items_quantity_merge_and_consumption():
    m1 = hs.parse_reply("<horae>\nitem:🍺Пиво(3 бутылки)|светлое=Лея@погреб\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\nitem:Пиво(1 бутылка)=Лея@погреб\n</horae>")[1]
    state = hs.replay(_entries(m1, m2))
    # Кириллическая единица количества — тот же предмет, не дубль.
    assert list(state["items"]) == ["Пиво(1 бутылка)"]
    item = state["items"]["Пиво(1 бутылка)"]
    assert item["id"] == "001" and item["description"] == "светлое" and item["icon"] == "🍺"
    m3 = hs.parse_reply("<horae>\nitem:Пиво(0 бутылок)=Лея@погреб\n</horae>")[1]
    assert hs.replay(_entries(m1, m3))["items"] == {}
    m4 = hs.parse_reply("<horae>\nitem-:Пиво\n</horae>")[1]
    assert hs.replay(_entries(m1, m4))["items"] == {}
    m5 = hs.parse_reply("<horae>\nitem:Пиво=израсходовано\n</horae>")[1]
    assert hs.replay(_entries(m1, m5))["items"] == {}


def test_replay_importance_never_downgrades_and_lock_protects():
    m1 = hs.parse_reply("<horae>\nitem!!:💎Камень|древний=Лея@сумка\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\nitem:🪨Камень|обычный=Вольф@стол\n</horae>")[1]
    state = hs.replay(_entries(m1, m2))
    stone = state["items"]["Камень"]
    assert stone["importance"] == "!!" and stone["holder"] == "Вольф"
    assert stone["icon"] == "🪨" and stone["description"] == "обычный"
    lock = {"id": "op_1", "seq": 1, "at": 1, "kind": "item.lock", "name": "Камень", "locked": True}
    state = hs.replay(_entries(m1, m2), ops=[lock])
    stone = state["items"]["Камень"]
    assert stone["icon"] == "💎" and stone["description"] == "древний" and stone["holder"] == "Вольф"


def test_replay_npc_protected_fields_and_age_ref():
    m1 = hs.parse_reply("<horae>\ntime:2020/1/1\nnpc:Вольф|шрам=молчалив@гость~gender:мужской~age:30\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\ntime:2021/6/1\nnpc:Вольф|~gender:женский~age:31\n</horae>")[1]
    npc = hs.replay(_entries(m1, m2))["npcs"]["Вольф"]
    assert npc["gender"] == "мужской"          # защищённое поле ИИ не перезаписывает
    assert npc["age"] == "31" and npc["age_ref"] == "2021/6/1"
    assert npc["appearance"] == "шрам" and npc["id"] == "001"


def test_replay_affection_set_and_add():
    m1 = hs.parse_reply("<horae>\naffection:Вольф=40\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\naffection:Вольф+5\n</horae>")[1]
    assert hs.replay(_entries(m1, m2))["affection"] == {"Вольф": 45.0}


def test_replay_agenda_done_is_not_destructive():
    m1 = hs.parse_reply("<horae>\nagenda:|Испечь пирог для Марины\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\nagenda-:пирог для Марины\n</horae>")[1]
    assert hs.replay(_entries(m1, m2))["agenda"] == []
    # Свайп на вариант без «agenda-» — план снова на месте (у плагина удаление было навсегда).
    assert [a["text"] for a in hs.replay(_entries(m1, None))["agenda"]] == ["Испечь пирог для Марины"]


def test_replay_skips_side_scene_and_respects_until():
    m1 = hs.parse_reply("<horae>\nlocation:Лес\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\nlocation:Сон\n</horae>")[1]
    m3 = hs.parse_reply("<horae>\nlocation:Город\n</horae>")[1]
    entries = [(1, "assistant", m1, False), (2, "assistant", m2, True), (3, "assistant", m3, False)]
    assert hs.replay(entries)["scene"]["location"] == "Город"
    assert hs.replay(entries, until=2)["scene"]["location"] == "Лес"


def test_ops_are_anchored_in_time():
    m1 = hs.parse_reply("<horae>\nnpc:Вольф|=молчалив@гость\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\nnpc:Вольф|=улыбается@друг\n</horae>")[1]
    op = {"id": "op_1", "seq": 1, "at": 1, "kind": "npc.set", "name": "Вольф",
          "fields": {"personality": "правка пользователя"}}
    # Правка после сообщения 1, ИИ обновил поле в сообщении 2 — новее ИИ.
    assert hs.replay(_entries(m1, m2), ops=[op])["npcs"]["Вольф"]["personality"] == "улыбается"
    # Правка после сообщения 2 — действует она.
    op2 = {**op, "at": 2}
    assert hs.replay(_entries(m1, m2), ops=[op2])["npcs"]["Вольф"]["personality"] == "правка пользователя"


def test_npc_rename_cascades_and_aliases_future_mentions():
    m1 = hs.parse_reply("<horae>\ncharacters:Незнакомец\ncostume:Незнакомец=плащ\n"
                        "npc:Незнакомец|высокий=@гость\naffection:Незнакомец=10\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\naffection:Незнакомец+5\n</horae>")[1]
    rename = {"id": "op_1", "seq": 1, "at": 1, "kind": "npc.rename", "from": "Незнакомец", "to": "Кай"}
    state = hs.replay(_entries(m1, m2), ops=[rename])
    assert "Незнакомец" not in state["npcs"] and state["npcs"]["Кай"]["aliases"] == ["Незнакомец"]
    assert state["affection"] == {"Кай": 15.0}
    assert state["costumes"] == {"Кай": "плащ"} and state["scene"]["characters"] == ["Кай"]


def test_npc_delete_blocks_ai_recreation_until_user_adds():
    m1 = hs.parse_reply("<horae>\nnpc:Толпа|шумная=@фон\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\nnpc:Толпа|=@фон\naffection:Толпа=5\n</horae>")[1]
    ops = [{"id": "op_1", "seq": 1, "at": 1, "kind": "npc.delete", "name": "Толпа"}]
    state = hs.replay(_entries(m1, m2), ops=ops)
    assert "Толпа" not in state["npcs"] and "Толпа" not in state["affection"]
    ops.append({"id": "op_2", "seq": 2, "at": 2, "kind": "npc.add", "name": "Толпа", "fields": {}})
    assert "Толпа" in hs.replay(_entries(m1, m2), ops=ops)["npcs"]


def test_user_relationship_and_location_are_not_overwritten_by_ai():
    m1 = hs.parse_reply("<horae>\nrel:А>Б=друзья\nlocation:Дом\nscene_desc:деревянный\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\nrel:А>Б=враги\nlocation:Дом\nscene_desc:каменный\n</horae>")[1]
    ops = [
        {"id": "op_1", "seq": 1, "at": 1, "kind": "rel.set", "from": "А", "to": "Б", "type": "соперники", "note": ""},
        {"id": "op_2", "seq": 2, "at": 1, "kind": "location.set", "name": "Дом", "desc": "из кирпича"},
    ]
    state = hs.replay(_entries(m1, m2), ops=ops)
    assert state["relationships"][0]["type"] == "соперники"
    assert state["locations"]["Дом"]["desc"] == "из кирпича"


def test_agenda_delete_blocks_ai_readd():
    m1 = hs.parse_reply("<horae>\nagenda:|Найти брата\n</horae>")[1]
    m2 = hs.parse_reply("<horae>\nagenda:|Найти брата\n</horae>")[1]
    ops = [{"id": "op_1", "seq": 1, "at": 1, "kind": "agenda.delete", "text": "Найти брата"}]
    assert hs.replay(_entries(m1, m2), ops=ops)["agenda"] == []


def test_seed_is_starting_point():
    seed = hs.replay(_entries(hs.parse_reply("<horae>\nlocation:Старый мир\nnpc:Кай|=@друг\n</horae>")[1]))
    seed["events"] = []
    state = hs.replay(_entries(hs.parse_reply("<horae>\ntime:2026/1/1\n</horae>")[1]), seed=seed)
    assert state["scene"]["location"] == "Старый мир" and "Кай" in state["npcs"]


# ----------------------------------------------------------------- хронология и рендер
def _chat():
    metas = []
    for n, (date, level, text) in enumerate([
        ("2026/2/1", "normal", "Пришли в таверну"),
        ("2026/2/2", "critical", "Вольф поклялся защищать Марину"),
        ("2026/2/3", "normal", "Помыли посуду"),
        ("2026/2/4", "normal", "Спустились в подвал"),
    ]):
        metas.append(hs.parse_reply(f"<horae>\ntime:{date} 10:00\nlocation:Таверна\n</horae>"
                                    f"<horaeevent>\nevent:{level}|{text}\n</horaeevent>")[1])
    return _entries(*metas)


def test_render_timeline_depth_and_summaries():
    state = hs.replay(_chat())
    text = hs.render_timeline(state, [], {"context_depth": 1})
    assert "★ #2 2026/2/2 10:00(позавчера): Вольф поклялся защищать Марину" in text
    assert "Спустились в подвал" in text and "Пришли в таверну" not in text
    summary = {"id": "s_1", "kind": "auto", "range": [1, 3], "text": "Сводка начала", "active": True,
               "date_from": "2026/2/1", "date_to": "2026/2/3"}
    text = hs.render_timeline(state, [summary], {"context_depth": 15})
    assert "📋 [Сводка·2026/2/1~2026/2/3](3 дня назад): Сводка начала" in text
    assert "Вольф поклялся" not in text and "Спустились в подвал" in text
    inactive = {**summary, "active": False}
    text = hs.render_timeline(state, [inactive], {"context_depth": 15})
    assert "Сводка начала" not in text and "Вольф поклялся" in text


def test_timeline_items_mark_covered_events():
    state = hs.replay(_chat())
    summary = {"id": "s_1", "kind": "auto", "range": [1, 2], "text": "x", "active": True}
    items = hs.timeline(state, [summary])
    covered = [i for i in items if i["kind"] == "event" and i["covered_by"] == "s_1"]
    assert [i["mid"] for i in covered] == [1, 2]
    assert items[0]["kind"] == "summary"


def test_render_state_block_sections():
    state = hs.replay(_entries(_meta()[1]))
    settings = hset.resolve()
    block = hs.render_state_block(state, settings, names=hs.Names("Лея", "Марина"))
    assert block.startswith("[Снимок текущего состояния")
    assert "[Время|2026/2/4 (ср) 15:00]" in block
    assert "[Сцена|Таверна·зал|шумно, пахнет элем]" in block
    assert "[Присутствуют|Вольф(кожаная куртка)|Марина(серый фартук)]" in block
    assert "#001 🗝Ключ от подвала[важно] | ржавый, тяжёлый = Марина@карман фартука" in block
    assert "[Расположение|Вольф:+40]" in block
    assert "N001 Вольф｜серая шерсть/зелёные глаза=молчалив@постоянный гость {{user}}" in block
    assert "~пол:мужской~возраст:35~профессия:наёмник" in block
    assert "[Список дел]" in block and "[Сюжетная линия]" in block
    # Модули выключены — их разделов нет.
    off = {**settings, "send_items": False, "send_affection": False, "send_agenda": False,
           "send_timeline": False}
    block = hs.render_state_block(state, off, names=hs.Names("Лея", "Марина"))
    assert "[Список предметов]" not in block and "[Расположение" not in block
    assert "[Список дел]" not in block and "[Сюжетная линия]" not in block


def test_main_character_personality_can_be_hidden():
    state = hs.replay(_entries(hs.parse_reply("<horae>\nnpc:Марина|рыжая=вспыльчива@подруга\n</horae>")[1]))
    settings = {**hset.resolve(), "send_main_personality": False}
    block = hs.render_state_block(state, settings, names=hs.Names("Лея", "Марина"))
    assert "Марина｜рыжая=@подруга" in block


def test_empty_state_renders_nothing():
    assert hs.render_state_block(hs.empty_state(), hset.resolve(), names=hs.Names()) == ""


def test_message_brief_and_document():
    meta = _meta()[1]
    brief = hs.message_brief(meta)
    assert brief.startswith("2026/2/4 15:00 · Таверна·зал · 2 перс. · ●Марина передала")
    doc = hs.build_document(meta)
    assert "Марина передала Вольфу ключ" in doc and "Таверна·зал Вольф Марина 2026/2/4 15:00" in doc
    assert "кожаная куртка" not in doc
    assert hs.message_brief(None) is None


def test_normalize_meta_from_ui():
    meta = hs.normalize_meta({
        "time": {"date": "2026/1/1", "time": "9:00"},
        "scene": {"characters": "А, Б , ", "location": "Дом"},
        "items": {"Меч": {"importance": "!!", "holder": "А"}, "": {"x": 1}},
        "events": [{"level": "важное", "text": "Событие"}, {"text": ""}],
        "affection": {"А": {"mode": "add", "value": "3"}, "Б": "x"},
        "junk": 1,
    })
    assert meta["scene"]["characters"] == ["А", "Б"]
    assert list(meta["items"]) == ["Меч"] and meta["items"]["Меч"]["importance"] == "!!"
    assert meta["events"] == [{"level": "important", "text": "Событие"}]
    assert meta["affection"] == {"А": {"mode": "add", "value": 3.0}}
    assert "junk" not in meta and meta["source"] == "user"


def test_to_api_lists_and_levels():
    state = hs.replay(_entries(_meta()[1]))
    api = hs.to_api(state, settings=hset.resolve(), names=hs.Names("Лея", "Марина"))
    assert api["time"]["display"] == "2026/2/4 (ср) 15:00"
    assert api["affection"] == [{"name": "Вольф", "value": 40.0, "level": "дружба"}]
    assert {n["name"] for n in api["npcs"]} == {"Вольф"}
    assert api["npcs"][0]["present"] is True
    assert api["rpg"] is None


def test_op_labels():
    assert hs.op_label({"kind": "npc.rename", "from": "А", "to": "Б"}) == "NPC «А» переименован в «Б»"
    assert hs.op_label({"kind": "rpg.bar", "owner": "Лея"}).startswith("RPG: bar")


# ----------------------------------------------------------------- настройки
def test_settings_layers_and_prompts_merge():
    g = {"send_mood": True, "context_depth": 99999, "prompts": {"system": "ГЛОБ"}}
    c = {"send_mood": False, "prompts": {"reminder": "ПЕРС"}}
    chat = {"rules_position": "tail", "prompts": {"system": ""}, "unknown": 1}
    s = hset.resolve(g, c, chat)
    assert s["send_mood"] is False
    assert s["context_depth"] == 500          # зажато
    assert s["rules_position"] == "tail"
    assert s["prompts"] == {"reminder": "ПЕРС"}  # "" вернул промпт по умолчанию


def test_settings_merge_overrides_null_removes():
    cur = {"send_mood": True, "recall_top_k": 3}
    out = hset.merge_overrides(cur, {"send_mood": None, "recall_top_k": 50, "bogus": 1})
    assert out == {"recall_top_k": 10}


def test_settings_sanitize_rpg_configs():
    s = hset.sanitize({"rpg_bar_config": [{"key": "HP!", "name": "Жизнь", "max": "abc"},
                                           {"key": "xp"}, {"key": "hp"}],
                       "rpg_user_only": ["bars", "nope"],
                       "calendar": {"enabled": True, "months": [{"name": "Иней", "days": 30}, {"name": ""}]}})
    assert s["rpg_bar_config"] == [{"key": "hp", "name": "Жизнь", "color": "#8aa3ff", "max": 100, "desc": ""}]
    assert s["rpg_user_only"] == ["bars"]
    assert s["calendar"] == {"enabled": True, "months": [{"name": "Иней", "days": 30}]}


# ----------------------------------------------------------------- промпты
def test_rules_prompt_includes_enabled_modules_only():
    base = hset.resolve()
    text = hp.rules_prompt(base, user="Лея", char="Марина")
    assert "【Система памяти Horae】" in text and "{{user}}" not in text and "Лея" in text
    assert "scene_desc:" not in text and "Сеть отношений" not in text
    full = {**base, "send_location_memory": True, "send_relationships": True, "send_mood": True,
            "anti_paraphrase": True}
    text = hp.rules_prompt(full, user="Лея", char="Марина", tables_suffix="\n★ Таблица «К»")
    assert "scene_desc:" in text and "Память сцен" in text and "Сеть отношений" in text
    assert "mood:" in text and "Правила пользовательских таблиц" in text and "★ Таблица «К»" in text
    assert "без повторного пересказа" in text


def test_custom_system_prompt_without_addition_slot_appends():
    s = {**hset.resolve(), "send_mood": True, "prompts": {"system": "Мой промпт для {{char}}"}}
    text = hp.rules_prompt(s, user="Лея", char="Марина")
    assert text.startswith("Мой промпт для Марина") and "Отслеживание эмоций" in text


def test_batch_prompt_and_split_russian_delimiter():
    text = hp.batch_prompt(hset.resolve(), [(12, "текст А"), (15, "текст Б")],
                           include={"npc": True}, user="Лея", char="Марина")
    assert "===сообщение#12===\nтекст А" in text and "npc:имя" in text
    reply = ("===сообщение#12===\n<horae>\ntime:1/1\n</horae>\n"
             "=== Message #15 ===\n<horaeevent>\nevent:normal|x\n</horaeevent>")
    parts = hp.split_batch_response(reply)
    assert set(parts) == {12, 15} and "time:1/1" in parts[12]


def test_extract_summary():
    assert hp.extract_summary("бла <horaesummary> Итог </horaesummary>") == "Итог"
    assert hp.extract_summary("без тега") is None
    try:
        hp.extract_summary("<horaesummary>оборвано")
    except hp.TruncatedSummary:
        pass
    else:
        raise AssertionError("обрыв должен быть ошибкой")


def test_compress_prompts_are_separate():
    s = hset.resolve()
    ev = hp.compress_prompt(s, "events", events="E1\nE2", fulltext="", count=2, user="Лея")
    ft = hp.compress_prompt(s, "fulltext", events="", fulltext="ПОЛНЫЙ", count=2, user="Лея")
    assert "Объедините следующие 2 событий" in ev and "Прочитайте полный текст" not in ev
    assert "Прочитайте полный текст" in ft and "ПОЛНЫЙ" in ft


def test_query_rewrite_and_npc_enrich_parsing():
    intent, qs = hp.parse_query_rewrite("INTENT: сцена в порту\nQ: где ключ\nQ: где ключ\nQ: кто такой Кай")
    assert intent == "сцена в порту" and qs == ["где ключ", "кто такой Кай"]
    assert hp.parse_npc_enrich('```json\n{"appearance": "рыжая", "age": 30, "x": 1}\n```') == {
        "appearance": "рыжая", "age": "30"}
    assert hp.parse_npc_enrich("нет json") == {}


def test_replay_skips_broken_meta_but_keeps_the_rest():
    good = hs.parse_reply("<horae>\nlocation:Лес\nnpc:Кай|=@друг\n</horae>")[1]
    broken = {"items": "не словарь", "scene": {"location": "Болото"}}
    state = hs.replay([(1, "assistant", good, False), (2, "assistant", broken, False)])
    assert "Кай" in state["npcs"]
