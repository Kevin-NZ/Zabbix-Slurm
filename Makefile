PYTHON ?= python3

.PHONY: all test template validate check sample clean

all: check

## Run the test suite (no Slurm installation required)
test:
	$(PYTHON) -m unittest discover -s tests -v

## Regenerate templates/slurm_cluster_7.0.xml from tools/build_template.py
template:
	$(PYTHON) tools/build_template.py

## Validate the generated template
validate:
	$(PYTHON) tools/validate_template.py

## Regenerate, validate and test
check: template validate test

## Print the collector output for the test fixtures
sample:
	$(PYTHON) bin/slurm_zabbix.py --mode all --slurm-bin-dir tests/fakebin \
		--no-cache --pretty

clean:
	find . -name '__pycache__' -type d -exec rm -rf {} +
