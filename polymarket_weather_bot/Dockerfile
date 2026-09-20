FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
CMD ["sh", "-c", "uvicorn weatherbot.web:app --host 0.0.0.0 --port ${PORT:-8000}"]
