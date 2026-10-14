# Chatbot image for local/demo use. Production runs on Azure App Service.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY Code/backend/requirements.txt /app/Code/backend/requirements.txt
RUN pip install -r /app/Code/backend/requirements.txt

COPY Code/backend/chatbot /app/Code/backend/chatbot
COPY Code/frontend /app/Code/frontend
COPY Data/sample_docs /app/Data/sample_docs

RUN useradd --create-home appuser && mkdir -p /app/.localdata && chown appuser /app/.localdata
USER appuser
WORKDIR /app/Code/backend
EXPOSE 8000
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=2", "--timeout=120", "--worker-class=uvicorn.workers.UvicornWorker", "chatbot.main:app"]
