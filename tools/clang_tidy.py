#!/usr/bin/env python3
"""Прогон clang-tidy по базе компиляции, которую пишет PlatformIO.

Зачем отдельный скрипт, а не вызов clang-tidy из Makefile.

Первое: база собрана для xtensa-gcc. В ней есть флаги, которых clang не знает —
-mlongcalls, -fstrict-volatile-bitfields, -fno-tree-switch-conversion. Для clang-tidy
неизвестный флаг это ошибка, и вместо замечаний получается

    error: unknown argument '-mlongcalls'
    error: 'config.h' file not found

то есть проверка молча не проверяет ничего. Здесь база очищается от чужих флагов, и
какие именно чужие — спрашиваем у самого clang, а не держим список руками: список
пришлось бы править при каждом обновлении платформы.

Второе: база знает ровно те файлы, что были в сборке. Проверять по ней файл, которого в
ней нет, бессмысленно — clang-tidy возьмёт команду по умолчанию, без -I проекта, и
упрётся в первый же include. Такие файлы перечисляются отдельно: это признак, что база
устарела и её надо пересобрать (pio run -t compiledb).

Третье: баз несколько (у каждой прошивки своя), и пути внутри них относительные — каждый
файл надо проверять по базе своего проекта.
"""

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys

# Плумбинг сборки: clang-tidy он не нужен, а -MMD ещё и пишет файл зависимостей.
DROP_FLAGS = {"-c", "-MMD", "-MD", "-MP"}
# Флаги, забирающие следующий аргумент.
DROP_WITH_VALUE = {"-o", "-MF", "-MT", "-MQ"}


def language_of(path):
    return "c" if path.endswith(".c") else "c++"


def flag_accepted(clang, flag, lang, cache):
    """Понимает ли clang этот флаг. Спрашиваем у него самого и запоминаем ответ."""
    key = (flag, lang)
    if key not in cache:
        probe = subprocess.run(
            [clang, "-fsyntax-only", flag, "-x", lang, os.devnull],
            capture_output=True,
            text=True,
        )
        # Нас интересует только «не знаю такого флага». Всё прочее (например, warning
        # на пустой вход) флаг не дискредитирует.
        bad = "unknown argument" in probe.stderr or "unsupported option" in probe.stderr
        cache[key] = not bad
    return cache[key]


def sanitize(entry, clang, cache):
    """Команда компиляции без флагов, которых clang не понимает."""
    argv = entry.get("arguments") or shlex.split(entry["command"])
    lang = language_of(entry["file"])
    out = [argv[0]]
    skip_value = False
    for arg in argv[1:]:
        if skip_value:
            skip_value = False
            continue
        if arg in DROP_WITH_VALUE:
            skip_value = True
            continue
        if arg in DROP_FLAGS or any(arg.startswith(p + "=") for p in DROP_WITH_VALUE):
            continue
        probe_it = arg.startswith("-") and not arg.startswith(("-I", "-D", "-U", "-isystem", "-include"))
        if probe_it and not flag_accepted(clang, arg, lang, cache):
            continue
        out.append(arg)
    return out


def prepare(project, out_root, clang, cache):
    """Пишет очищенную базу проекта и возвращает (каталог базы, множество файлов в ней)."""
    db_path = project / "compile_commands.json"
    entries = json.loads(db_path.read_text(encoding="utf-8"))
    clean = []
    files = set()
    for entry in entries:
        directory = entry.get("directory") or str(project)
        clean.append(
            {
                "directory": directory,
                "file": entry["file"],
                "arguments": sanitize(entry, clang, cache),
            }
        )
        files.add(os.path.normpath(os.path.join(directory, entry["file"])))
    out_dir = out_root / project.name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "compile_commands.json").write_text(json.dumps(clean, indent=1), encoding="utf-8")
    return out_dir, files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", action="append", default=[], help="каталог с compile_commands.json")
    ap.add_argument("--out", default=".lint", help="куда класть очищенные базы")
    ap.add_argument("--tidy", default="clang-tidy")
    ap.add_argument("--clang", default="clang")
    ap.add_argument("files", nargs="*", help="проверяемые файлы")
    args = ap.parse_args()

    out_root = pathlib.Path(args.out)
    cache = {}
    bases = []
    for name in args.project:
        project = pathlib.Path(name)
        if (project / "compile_commands.json").is_file():
            bases.append((project, *prepare(project, out_root, args.clang, cache)))

    if not bases:
        print("clang-tidy: compile_commands.json не найден (соберите базу: pio run -t compiledb)")
        return 0

    def base_for(path):
        """База проекта, которому файл принадлежит; иначе любая, которая его знает.

        Порядок важен: база tdeck перечисляет и файлы meshcore-fork (их туда занесла
        сборка T-Deck), но с путями включений от T-Deck. Проверять файл форка по ней —
        это «config.h not found» вместо замечаний.
        """
        own = [b for b in bases if path.startswith(os.path.abspath(str(b[0])) + os.sep)]
        for _project, out_dir, files in own + bases:
            if path in files:
                return out_dir
        return None

    checked, unknown, stale = 0, [], []
    for name in args.files:
        path = os.path.abspath(name)
        out_dir = base_for(path)
        if out_dir is None:
            unknown.append(name)
            continue
        print("tidying %s" % name)
        run = subprocess.run([args.tidy, "-p", str(out_dir), "--quiet", name], capture_output=True, text=True)
        sys.stdout.write(run.stdout)
        sys.stderr.write(run.stderr)
        checked += 1
        # Запись в базе есть, но по ней уже не собирается: обычно это устаревшие пути
        # включений. Отличать такое от настоящих замечаний важно — иначе файл выглядит
        # проверенным, а проверен не был.
        if "clang-diagnostic-error" in run.stdout + run.stderr:
            stale.append(name)

    print("\nclang-tidy: проверено %d файлов" % checked)
    # Это не придирка, а единственный признак, что проверка прошла мимо кода: молча
    # пропущенный файл выглядит ровно как файл без замечаний.
    for title, names in (
        ("базы компиляции не знают", unknown),
        ("в базе есть, но по ней не собираются (база устарела)", stale),
    ):
        if names:
            print("%s %d файлов — пересоберите базы (pio run -t compiledb):" % (title, len(names)))
            for name in names:
                print("  %s" % name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
