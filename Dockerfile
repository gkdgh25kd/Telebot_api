FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements-telegram-helper.txt

CMD ["python3", "telegram_api_helper.py"]
