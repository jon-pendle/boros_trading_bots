FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY strategies/ strategies/
COPY main.py .

VOLUME ["/app/logs"]

CMD ["python", "main.py", "--live"]
