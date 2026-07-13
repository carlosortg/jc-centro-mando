FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Volumen persistente para el SQLite (montar /data en Coolify)
ENV DATA_DIR=/data
ENV PORT=8090
EXPOSE 8090

# Healthcheck para Coolify
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/health').status==200 else 1)" || exit 1

CMD ["python", "app.py"]
