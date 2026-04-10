# Używamy lekkiego obrazu Pythona
FROM python:3.9-slim

# Ustawiamy katalog roboczy w kontenerze
WORKDIR /app

# Kopiujemy plik z listą zależności
COPY requirements.txt .

# Instalujemy zależności
RUN pip install --no-cache-dir -r requirements.txt

# Instalujemy przeglądarki dla Playwright
RUN playwright install chromium
RUN playwright install-deps

# Kopiujemy resztę plików aplikacji
COPY . .

# Informujemy, że aplikacja będzie nasłuchiwać na porcie 8000
EXPOSE 8000

# Komenda do uruchomienia serwera
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]