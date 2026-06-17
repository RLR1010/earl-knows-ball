FROM postgres:16-alpine
RUN apk add --no-cache --virtual .build-deps build-base git postgresql-dev llvm19-dev clang19 \
    && cd /tmp \
    && git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git \
    && cd pgvector \
    && make USE_PGXS=1 \
    && make USE_PGXS=1 install \
    && cd / \
    && rm -rf /tmp/pgvector \
    && apk del .build-deps
