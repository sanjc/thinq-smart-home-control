.PHONY: setup run lint clean

setup:
	python -m venv .venv
	. .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt

run:
	. .venv/bin/activate && python app.py

lint:
	. .venv/bin/activate && ruff check .
	. .venv/bin/activate && python -m compileall app.py

clean:
	rm -rf .venv __pycache__
