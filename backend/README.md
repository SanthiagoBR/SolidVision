# SolidVision Backend

This directory contains the initial backend structure for SolidVision.

It follows the Clean Architecture layout described in the project documentation and contains only placeholder modules and package scaffolding.

## Python version
This project targets Python 3.12.

## Create a virtual environment
```bash
python -m venv .venv
```

## Activate the environment
On Windows PowerShell:
```powershell
.\.venv\Scripts\Activate.ps1
```

On Bash:
```bash
source .venv/bin/activate
```

## Install dependencies
```bash
pip install -r backend/requirements.txt
```

## Run Ruff
```bash
ruff check backend
```

## Run Black
```bash
black backend
```

## Run MyPy
```bash
mypy backend
```

## Run Pytest
```bash
pytest backend/tests
```
