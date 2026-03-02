FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-common libreoffice-core \
    ca-certificates fonts-dejavu fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Poetry бўлса – шу ишлайди (сизда аввал Poetry ишлагани кўриниб турибди)
COPY pyproject.toml poetry.lock* /app/
RUN pip install --no-cache-dir -U pip \
 && pip install --no-cache-dir poetry \
 && poetry config virtualenvs.create false \
 && poetry install --only main --no-interaction --no-ansi

COPY . /app

CMD ["python", "main.py"]
