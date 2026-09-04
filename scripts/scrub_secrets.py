# -*- coding: utf-8 -*-
"""
Очистка секретов из базы после переноса их в переменные окружения.

Пароль администратора, код доступа и токен Telegram-бота хранились в таблице
app_settings открытым текстом. Резервная копия базы, её экспорт или чужой доступ
к файлу означали компрометацию всего сразу. После того как значения прописаны
в .env (см. ADMIN_PASSWORD, ACCESS_CODE, TELEGRAM_BOT_TOKEN), копии в базе
становятся лишними — и опасными.

Скрипт НИЧЕГО не делает молча: показывает, что нашёл, требует подтверждения и
делает резервную копию базы рядом. Значения на экран не печатаются.

Запуск:
    python scripts/scrub_secrets.py                 # спросит подтверждение
    python scripts/scrub_secrets.py --yes           # без вопросов
    python scripts/scrub_secrets.py --db путь.db    # другая база
"""
import argparse
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time

FIELDS = {
    "security": ("admin_password", "access_code"),
    "telegram": ("token",),
}
ENV_FOR = {
    "admin_password": "ADMIN_PASSWORD",
    "access_code": "ACCESS_CODE",
    "token": "TELEGRAM_BOT_TOKEN",
}


def env_value(name: str) -> str:
    """
    Значение переменной: сперва окружение процесса, затем файл .env.

    ПОЧЕМУ НЕ ПРОСТО os.environ: приложение читает настройки через
    pydantic-settings из файла .env, и в окружение процесса они НЕ попадают.
    Первая версия проверки смотрела только в os.environ и потому говорила
    «НЕ ЗАДАН» даже тогда, когда всё было прописано верно. Предупреждение было
    ложным, а доверия к нему требовалось как к настоящему — и один раз оно
    обошлось потерей кода доступа и токена бота.
    """
    val = (os.environ.get(name) or "").strip()
    if val:
        return val
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return ""
    try:
        text = io.open(path, encoding="utf-8").read()
    except OSError:
        return ""
    m = re.search(r"^%s\s*=\s*(.*)$" % re.escape(name), text, re.M)
    if not m:
        return ""
    return m.group(1).strip().strip('"').strip("'")


def mask(value: str) -> str:
    """Показываем факт наличия, а не значение."""
    if not value:
        return "(пусто)"
    return "задано, %d символов" % len(value)


def main() -> int:
    ap = argparse.ArgumentParser(description="Убрать секреты из app_settings")
    ap.add_argument("--db", default="data/aichat.db", help="путь к базе")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    ap.add_argument("--force", action="store_true",
                    help="стереть даже то, чего нет в .env (значения будут ПОТЕРЯНЫ)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("База не найдена: %s" % args.db)
        return 2

    con = sqlite3.connect(args.db)
    rows = {k: v for k, v in con.execute("select key, value from app_settings").fetchall()}

    found = []
    for key, names in FIELDS.items():
        raw = rows.get(key)
        if not raw:
            continue
        data = json.loads(raw) if isinstance(raw, str) else raw
        for name in names:
            if (data.get(name) or "").strip():
                found.append((key, name, data[name]))

    if not found:
        print("Секретов в базе нет — чистить нечего.")
        con.close()
        return 0

    print("Найдено в базе:")
    for key, name, value in found:
        env = ENV_FOR.get(name, "")
        in_env = bool(env_value(env))
        print("  %-9s %-15s %-22s  %s"
              % (key, name, mask(value),
                 ("%s задан" % env) if in_env else ("%s НЕ ЗАДАН — будет потеряно" % env)))

    missing = [n for _, n, _ in found if not env_value(ENV_FOR.get(n, ""))]
    if missing:
        print("\nОСТАНОВЛЕНО: эти значения нигде не продублированы:")
        for n in missing:
            print("    %-16s -> %s" % (n, ENV_FOR.get(n, "")))
        print("Очистка сделала бы их недоступными. Пропишите их в .env и повторите.")
        if not args.force:
            # ОТКАЗ, а не вопрос. Предупреждение легко проскочить: подтверждение
            # набирается на автомате, а потерянный код доступа означает открытое
            # наружу приложение. Чтобы стереть сознательно — явный --force.
            print("Если потеря значений входит в намерение, повторите с --force.")
            con.close()
            return 3
        print("Задан --force: продолжаю, значения будут потеряны.")

    if not args.yes:
        ans = input("\nОчистить эти поля в базе? Введите «да» для подтверждения: ").strip().lower()
        if ans not in ("да", "yes", "y"):
            print("Отменено, база не изменена.")
            con.close()
            return 1

    backup = "%s.bak-secrets-%s" % (args.db, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy(args.db, backup)
    print("Резервная копия: %s" % backup)

    for key, names in FIELDS.items():
        raw = rows.get(key)
        if not raw:
            continue
        data = json.loads(raw) if isinstance(raw, str) else raw
        changed = False
        for name in names:
            if (data.get(name) or "").strip():
                data[name] = ""
                changed = True
        if changed:
            con.execute("update app_settings set value=? where key=?", (json.dumps(data), key))
    con.commit()
    con.close()
    print("Готово: поля очищены. Значения теперь берутся только из окружения.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
