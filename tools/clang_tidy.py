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

Четвёртое: стандартные заголовки компилятор знает сам, и в базе их нет. У xtensa-gcc это
newlib и его libstdc++, у clang — ХОСТОВЫЕ /usr/include. Поэтому clang разбирал наш код
против glibc, получал «function-like macro '__GLIBC_USE' is not defined» и «unknown type name
'__mbstate_t'», а после десятка таких — «too many errors emitted, stopping now»: файл выглядел
проверенным, а настоящие замечания в нём не считались вовсе (761 clang-diagnostic-error на
полном прогоне, все в /usr/include). Лечится тем, что стандартный путь включений вырезается
(-nostdinc), а вместо него подставляются каталоги ЦЕЛЕВОГО компилятора — и спрашиваются они у
него самого (`-E -v`), а не прописываются руками: версия тулчейна меняется с платформой.
"""

import argparse
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

# Плумбинг сборки: clang-tidy он не нужен, а -MMD ещё и пишет файл зависимостей.
DROP_FLAGS = {"-c", "-MMD", "-MD", "-MP"}
# Флаги, забирающие следующий аргумент.
DROP_WITH_VALUE = {"-o", "-MF", "-MT", "-MQ"}
# Запасной список на случай, если спросить у clang нельзя (его нет рядом с clang-tidy —
# так бывает в CI, где ставят только clang-tidy). Это флаги xtensa-gcc, на которых clang
# останавливается с «unknown argument». Список заведомо неполный: он нужен ровно чтобы
# проверка не падала целиком, а основной путь — проба у самого clang.
GCC_ONLY_PREFIXES = ("-mlongcalls", "-mtext-section-literals", "-mfix-esp32", "-fno-tree-", "-fstrict-volatile-")


def pio_core():
    """Каталог пакетов PlatformIO: там живут тулчейны."""
    return pathlib.Path(os.environ.get("PLATFORMIO_CORE_DIR") or os.path.expanduser("~/.platformio"))


def resolve_compiler(argv0):
    """Абсолютный путь к компилятору из базы или None.

    В базе стоит голое имя (`xtensa-esp32s3-elf-g++`), и на PATH его обычно нет: PlatformIO
    держит тулчейны у себя и добавляет их в PATH только на время сборки. Поэтому после PATH
    смотрим в пакеты PlatformIO — у каждой платы свой тулчейн (esp32, esp32s3), и зашивать
    имя нельзя.
    """
    name = os.path.basename(argv0)
    found = shutil.which(argv0) or shutil.which(name)
    if found:
        return found
    for exe in sorted(pio_core().glob("packages/toolchain-*/bin/" + name)):
        if os.access(exe, os.X_OK):
            return str(exe)
    return None


def _ask(compiler, extra, lang):
    """Вывод компилятора на пустом входе. Пусто — компилятор не ответил."""
    try:
        probe = subprocess.run(
            [compiler, *extra, "-x", lang, os.devnull],
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return probe


def _search_dirs(stderr):
    """Каталоги из блока «#include <...> search starts here» вывода `-E -v`."""
    dirs, grab = [], False
    for line in stderr.splitlines():
        if line.startswith("#include <...>"):
            grab = True
        elif line.startswith("End of search list"):
            break
        elif grab and line.startswith(" "):
            d = os.path.normpath(line.strip())
            if os.path.isdir(d):
                dirs.append(d)
    return dirs


def _predefines(compiler, lang):
    """Макросы, которые компилятор определяет сам: {имя: значение}."""
    probe = _ask(compiler, ["-dM", "-E"], lang)
    if probe is None or probe.returncode != 0:
        return {}
    out = {}
    for line in probe.stdout.splitlines():
        m = re.match(r"#define (\S+)(?: (.*))?$", line)
        if m:
            out[m.group(1)] = m.group(2) or ""
    return out


def target_env(argv0, clang, lang, cache):
    """Флаги, которыми clang притворяется целевым компилятором. Пусто — притворяться нечем.

    Три вещи, которых в базе компиляции нет, потому что компилятор знает их сам:

    1. **Стандартные заголовки.** У xtensa-gcc это newlib и его libstdc++, у clang —
       хостовые /usr/include. Поэтому `-nostdinc` плюс каталоги целевого компилятора,
       спрошенные у него же (`-E -v`).
    2. **Разрядность.** Указатель на цели 4 байта, на хосте 8. Без `-m32` хостовый clang
       честно ругается на приведение указателя к uint32_t в заголовках ESP-IDF — это не
       наш баг, а разница моделей.
    3. **Опознавательные макросы.** Заголовки ветвятся по `__XTENSA__`: в ветке «не
       xtensa» лежат includes вида `"../hal.h"`, которых в этой раскладке SDK нет, и
       разбор падает с «file not found» на первом же Arduino.h.

    Макросы берутся не списком, а разностью: что целевой компилятор определяет, а clang
    нет. Из разности оставляются только **признаки** (значение пустое или 1) — те, по
    которым ветвятся `#ifdef`. Числовые пределы (`__FLT32_MAX__`, `__DEC64_*`) отбрасываются
    намеренно: подменять ими модель чисел clang'а незачем, а сломать разбор заголовков ими
    легко. Такой отбор переживёт смену тулчейна — для esp32c3 он даст `__riscv` сам, без
    правки скрипта.
    """
    key = ("env", argv0, lang)
    if key in cache:
        return cache[key]
    compiler = resolve_compiler(argv0)
    flags = []
    if compiler:
        probe = _ask(compiler, ["-E", "-v"], lang)
        dirs = _search_dirs(probe.stderr) if probe is not None else []
        if dirs:
            flags.append("-nostdinc")
            for d in dirs:
                flags += ["-isystem", d]
            target = _predefines(compiler, lang)
            host = _predefines(clang, lang)
            if host and target.get("__SIZEOF_POINTER__") == "4" and flag_accepted(clang, "-m32", lang, cache):
                flags.append("-m32")
            for name, value in sorted(target.items()):
                if name not in host and value in ("", "1"):
                    flags.append("-D%s=1" % name if value else "-D" + name)
    if not flags and not cache.get("__env_warned"):
        print(
            "clang-tidy: %r не найден или не отвечает — разбор пойдёт против ХОСТОВЫХ "
            "заголовков, и часть замечаний не посчитается" % argv0
        )
        cache["__env_warned"] = True
    cache[key] = flags
    return flags


def language_of(path):
    return "c" if path.endswith(".c") else "c++"


def flag_accepted(clang, flag, lang, cache):
    """Понимает ли clang этот флаг. Спрашиваем у него самого и запоминаем ответ."""
    key = (flag, lang)
    if key not in cache:
        try:
            probe = subprocess.run(
                [clang, "-fsyntax-only", flag, "-x", lang, os.devnull],
                capture_output=True,
                text=True,
            )
        except OSError:
            # clang рядом нет — спросить не у кого. Молчать об этом нельзя: тогда
            # непонятно, почему проверка вдруг стала выдавать «unknown argument».
            if not cache.get("__warned"):
                print("clang-tidy: %r не запускается, флаги отбираю по запасному списку" % clang)
                cache["__warned"] = True
            cache[key] = not flag.startswith(GCC_ONLY_PREFIXES)
            return cache[key]
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
    # Чем clang отличается от целевого компилятора: заголовки, разрядность, опознавательные
    # макросы. Место флагов в строке не важно — -isystem системный, и clang смотрит его
    # ПОСЛЕ всех -I проекта, поэтому свои заголовки по-прежнему находятся первыми.
    out += target_env(argv[0], clang, lang, cache)
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
