PYTHON ?= python3

.PHONY: all build check clean

all: build

build:
	$(PYTHON) tools/build.py

check: build
	$(PYTHON) tools/build.py --check

clean:
	$(PYTHON) tools/build.py --clean
