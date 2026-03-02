FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# LibreOffice + fontlar
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-common libreoffice-core \
    ca-certificates fonts-dejavu fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Аввал кодни оламиз (COPY’da файл йўқ деб қулаши бўлмайди)
COPY . /app

# Dependency install: Poetry борми? requirements*.txt борми?
RUN pip install --no-cache-dir -U pip \
 && if [ -f pyproject.toml ]; then \
      pip install --no-cache-dir poetry && \
      poetry config virtualenvs.create false && \
      poetry install --only main --no-interaction --no-ansi ; \
    else \
      REQ="$(ls -1 requirements*.txt 2>/dev/null | head -n 1)" ; \
      if [ -n "$REQ" ]; then \
        pip install --no-cache-dir -r "$REQ" ; \
      else \
        echo "ERROR: pyproject.toml ham requirements*.txt ham topilmadi. Dependency fayl qo‘shing." && exit 1 ; \
      fi ; \
    fi

CMD ["python", "main.py"]
