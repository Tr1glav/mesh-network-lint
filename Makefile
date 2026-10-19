# Линтер и форматтер для ESP32 Mesh Network
#
# Цели:
#   make lint          — проверить C++ и Python
#   make format        — отформатировать C++ и Python
#   make format-check  — проверить, что форматирование чистое
#
# C++: clang-format + clang-tidy
# Python: ruff

# Берём ровно тот clang-format, что на PATH. Предпочтение clang-format-18 отсюда убрано:
# его в системе нет, а если появится — форматтер молча станет другим, чем у pre-commit и
# у соседа. Опции, которые версии понимают по-разному, закреплены в .clang-format явно.
CXX_FORMAT := $(shell command -v clang-format 2>/dev/null || echo "clang-format")
CXX_LINT   := $(shell command -v clang-tidy 2>/dev/null || echo "clang-tidy")
PY_LINT    := $(shell command -v ruff 2>/dev/null || echo "ruff")

# Файлы для форматирования C++. tdeck/lib/meshcore — своя копия общей библиотеки, и она
# тоже сюда входит: без неё копии расходились по одному форматированию, и проверка
# расхождений в selftest падала на пробелах.
CXX_ALL = $(wildcard mesh-network-core/src/*.cpp mesh-network-core/include/*.h) \
	$(wildcard meshcore-fork/lib/meshcore/src/*.cpp meshcore-fork/lib/meshcore/include/*.h) \
	$(wildcard tdeck/src/*.cpp tdeck/include/*.h) \
	$(wildcard tdeck/apps/*/*.cpp tdeck/apps/*/*.h) \
	$(wildcard tdeck/lib/meshcore/src/*.cpp tdeck/lib/meshcore/include/*.h)

# Сгенерированные заголовки не форматируем: их вид задаёт генератор, и правка руками (в
# том числе форматтером) исчезнет при следующей сборке, а format-check снова покраснеет.
# То же перечислено в .clang-format-ignore — для редактора и pre-commit, которые ходят
# мимо Makefile.
CXX_GENERATED = \
	meshcore-fork/lib/meshcore/include/build_info.h \
	meshcore-fork/lib/meshcore/include/ca_bundle.h \
	meshcore-fork/lib/meshcore/include/cyrillic_glyphs.h \
	meshcore-fork/lib/meshcore/include/web_assets.h \
	tdeck/lib/meshcore/include/build_info.h \
	tdeck/lib/meshcore/include/cyrillic_glyphs.h

CXX_FILES = $(filter-out $(CXX_GENERATED),$(CXX_ALL))

# Каталоги с НАШИМ Python. Раньше здесь был PY_FILES со списком файлов, и он не
# использовался ни одной целью: цели перечисляли пути сами, только mesh-network-tools, —
# то есть 18 своих файлов (скрипты сборки обеих прошивок, host_render, tools/) конвейер не
# видел вовсе. `make lint-py` давал «All checks passed», пока `ruff check .` из корня
# показывал 23 замечания ровно в этих файлах.
#
# Каталогами, а не списком файлов: новый скрипт в scripts/ тогда попадает под проверку сам,
# без правки Makefile. Чужой Python из .pio/libdeps ruff не берёт — он в .gitignore прошивок.
PY_DIRS_ALL = mesh-network-tools \
	mesh-network-tools/mcselftest \
	meshcore-fork/scripts \
	tdeck/scripts \
	tdeck/host_render \
	tools

# ...но ruff падает с E902 на пути, которого нет, а в CI рядом лежит РОВНО ОДНА прошивка:
# workspace собирается из этого репозитория в корне, прошивки и ядра подкаталогами рядом
# (см. .github/workflows/build.yml прошивок). Список без фильтра означал бы, что сборка
# tdeck спотыкается на meshcore-fork/scripts и наоборот — то есть красный CI у обеих.
# $(wildcard) оставляет только существующие каталоги; локально это все шесть.
PY_DIRS = $(wildcard $(PY_DIRS_ALL))

.PHONY: lint format format-check format-cpp format-cpp-check lint-cpp format-py \
	format-py-check lint-py

lint: lint-cpp lint-py

format: format-cpp format-py

format-check: format-cpp-check format-py-check

format-cpp:
	@for f in $(CXX_FILES); do \
		echo "formatting $$f"; \
		$(CXX_FORMAT) -i $$f; \
	done

format-py:
	@$(PY_LINT) format $(PY_DIRS)

format-cpp-check:
	@for f in $(CXX_FILES); do \
		$(CXX_FORMAT) --dry-run --Werror $$f || { echo "FAIL: $$f"; exit 1; }; \
	done
	@echo "C++ formatting check passed"

format-py-check:
	@$(PY_LINT) format --check $(PY_DIRS)
	@echo "Python formatting check passed"

# База компиляции у каждой прошивки своя, пути в ней относительные, а флаги — от
# xtensa-gcc, которых clang не знает. Разбор этого вынесен в tools/clang_tidy.py: там же
# и перечисление файлов, которых база не знает, — иначе пропущенный файл не отличить от
# файла без замечаний. Замечания печатаются, но сборку не валят: так было и раньше.
lint-cpp:
	@python3 tools/clang_tidy.py \
		--tidy "$(CXX_LINT)" \
		--project tdeck --project meshcore-fork \
		$(CXX_FILES)

lint-py:
	@$(PY_LINT) check $(PY_DIRS)

