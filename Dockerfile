# Chromium нужен для автоматического обнаружения API карты при запуске.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    GPN_DB=/data/gpn.db

WORKDIR /app

# зависимости отдельным слоем — переустанавливаются только при их изменении
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

COPY *.py ./

# база лежит в томе, чтобы подписки и история переживали пересборку
RUN mkdir -p /data && useradd -r -u 1000 gpn && chown -R gpn /app /data
USER gpn
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=30s \
    CMD python -c "import sqlite3,os,time,sys; \
db=sqlite3.connect(os.environ['GPN_DB']); \
r=db.execute('SELECT MAX(checked_at) FROM fuel_state').fetchone()[0]; \
sys.exit(0 if r and time.time()-r < 1800 else 1)"

CMD ["python", "bot.py"]
