FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libreoffice-core libreoffice-writer libreoffice-common \
    fonts-dejavu fonts-liberation \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# LibreOffice бинарниклари шу ерда бўлади, PATH'га қўшиб қўямиз
ENV PATH="/usr/lib/libreoffice/program:${PATH}"

# баъзи системаларда /usr/bin/soffice бўлмаслиги мумкин — мана шу 100% қилади
RUN ln -sf /usr/lib/libreoffice/program/soffice /usr/bin/soffice || true

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "main.py"]
