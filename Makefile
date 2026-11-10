PYTHON ?= python
PORT ?= 8000

.PHONY: prepare test report run smoke clean

prepare:
	$(PYTHON) -m src.prepare

test: prepare
	$(PYTHON) -m tests.label_report
	$(PYTHON) -m unittest discover -s tests -v

report: prepare
	$(PYTHON) -m tests.label_report

run: prepare
	$(PYTHON) -m uvicorn src.api:app --host 0.0.0.0 --port $(PORT)

smoke: prepare
	$(PYTHON) -m scripts.smoke

clean:
	$(PYTHON) -c "import shutil; shutil.rmtree('build', ignore_errors=True)"
