FROM earl-knows-football-api:latest
RUN pip install --no-cache-dir setuptools
COPY app/context_processor.py /app/app/context_processor.py
RUN rm -rf /app/app/__pycache__ /app/app/routers/__pycache__ 2>/dev/null; find /app -name '*.pyc' -delete 2>/dev/null; true
