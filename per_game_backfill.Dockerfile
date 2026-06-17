FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir httpx sqlalchemy asyncpg psycopg2-binary python-dotenv

COPY backend/app/ingestion/per_game_backfill.py /app/app/ingestion/per_game_backfill.py

CMD ["python", "-u", "-m", "app.ingestion.per_game_backfill", "--sport", "mlb", "--start", "2021", "--end", "2026", "--closing-only"]
