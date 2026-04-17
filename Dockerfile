FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements-telegram-helper.txt

RUN chmod +x start.sh.

CMD ["./start.sh."]
