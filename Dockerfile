FROM python:3.11-slim

WORKDIR /app

# Копируем зависимости и код
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Бот будет читать переменные окружения из .env или системных
CMD ["python", "bot.py"]
