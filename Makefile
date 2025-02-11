BIN := .tox/py3/bin
PY := $(BIN)/python
PIP := $(BIN)/pip
VERSION=$(shell cat VERSION)

clean:
	find . -name __pycache__ -type d -exec rm -r {} +
	find . -name *.pyc -delete
	rm -rf .tox
	rm -rf docs/_build/

.tox:
	tox -r --notest

client: .tox
	$(PY) -m juju.client.facade -s "juju/client/schemas*" -o juju/client/

test:
	tox

.PHONY: lint
lint: 
	tox -e lint --notest

docs: .tox
	$(PIP) install -r docs/requirements.txt
	rm -rf docs/_build/
	$(BIN)/sphinx-build -b html docs/  docs/_build/
	cd docs/_build/ && zip -r docs.zip *

release:
	git fetch --tags
	rm dist/*.tar.gz
	$(PY) setup.py sdist
	$(BIN)/twine upload --repository-url https://upload.pypi.org/legacy/ dist/*
	git tag ${VERSION}
	git push --tags

upload: release


.PHONY: clean client test docs upload release
