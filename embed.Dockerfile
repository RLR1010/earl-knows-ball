FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir httpx sqlalchemy psycopg2-binary

COPY backend/run_embed_pgvector_all.py /app/

CMD ["python", "-u", "run_embed_pgvector_all.py"]
