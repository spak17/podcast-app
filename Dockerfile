FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

# Skopiuj tylko requirements, aby wykorzystać cache warstw
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Skopiuj resztę plików
COPY . .

# Ustaw zmienne środowiskowe dla Playwright (ważne!)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV NODE_OPTIONS="--max-old-space-size=256"

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]