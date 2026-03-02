FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# LibreOffice + fontlar (PDF’да ёзувлар тўғри чиқиши учун)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-common libreoffice-core \
    ca-certificates fonts-dejavu fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency install (Poetry бўлса — Poetry, бўлмаса requirements.txt)
COPY pyproject.toml poetry.lock* /app/
COPY requirements.txt* /app/

RUN pip install --no-cache-dir -U pip \
 && if [ -f pyproject.toml ]; then \
      pip install --no-cache-dir poetry && \
      poetry config virtualenvs.create false && \
      poetry install --only main --no-interaction --no-ansi ; \
    elif [ -f requirements.txt ]; then \
      pip install --no-cache-dir -r requirements.txt ; \
    else \
      echo "ERROR: pyproject.toml ҳам requirements.txt ҳам йўқ" && exit 1 ; \
    fi

COPY . /app

CMD ["python", "main.py"]
