# Линтер и форматтер для ESP32 Mesh Network
#
# Цели:
#   make lint          — проверить C++ и Python
#   make format        — отформатировать C++ и Python
#   make format-check  — проверить, что форматирование чистое
#
# C++: clang-format + clang-tidy
# Python: ruff

CXX_FORMAT := $(shell command -v clang-format-18 2>/dev/null || command -v clang-format 2>/dev/null || echo "clang-format")
CXX_LINT   := $(shell command -v clang-tidy 2>/dev/null || echo "clang-tidy")
PY_LINT    := $(shell command -v ruff 2>/dev/null || echo "ruff")

# Файлы для форматирования C++
CXX_FILES = $(wildcard mesh-network-core/src/*.cpp mesh-network-core/include/*.h) \
	$(wildcard meshcore-fork/lib/meshcore/src/*.cpp meshcore-fork/lib/meshcore/include/*.h) \
	$(wildcard tdeck/src/*.cpp tdeck/include/*.h) \
	$(wildcard tdeck/apps/*/*.cpp tdeck/apps/*/*.h)

# Файлы для форматирования Python
PY_FILES  = $(wildcard mesh-network-tools/*.py) \
	$(wildcard mesh-network-tools/mcselftest/*.py)

.PHONY: lint format format-check format-cpp lint-cpp format-py lint-py

lint: lint-cpp lint-py

format: format-cpp format-py

format-check: format-cpp-check format-py-check

format-cpp:
	@for f in $(CXX_FILES); do \
		echo "formatting $$f"; \
		$(CXX_FORMAT) $$f; \
	done

format-py:
	@$(PY_LINT) format mesh-network-tools/ mesh-network-tools/mcselftest/

format-cpp-check:
	@for f in $(CXX_FILES); do \
		$(CXX_FORMAT) --dry-run --Werror $$f || { echo "FAIL: $$f"; exit 1; }; \
	done
	@echo "C++ formatting check passed"

format-py-check:
	@$(PY_LINT) format --check mesh-network-tools/ mesh-network-tools/mcselftest/
	@echo "Python formatting check passed"

lint-cpp:
	@if test -f meshcore-fork/.pio/build/tdeck/compile_commands.json; then \
		for f in $(CXX_FILES); do \
			echo "tidying $$f"; \
			$(CXX_LINT) -p meshcore-fork/.pio/build/tdeck $$f 2>&1; \
		done; \
	else \
		echo "clang-tidy: compile_commands.json not found (run 'pio run' first)"; \
	fi

lint-py:
	@$(PY_LINT) check mesh-network-tools/ mesh-network-tools/mcselftest/

