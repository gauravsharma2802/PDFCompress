FROM python:3.12-slim AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        automake \
        autoconf \
        libtool \
        pkg-config \
        ca-certificates \
        git \
        libleptonica-dev \
        zlib1g-dev \
        libpng-dev \
    && git clone https://github.com/agl/jbig2enc.git /tmp/jbig2enc \
    && cd /tmp/jbig2enc \
    && ./autogen.sh \
    && ./configure \
    && make \
    && make install

FROM python:3.12-slim

# Copy jbig2enc binary and libs from builder
COPY --from=builder /usr/local/bin/jbig2 /usr/local/bin/jbig2
COPY --from=builder /usr/local/lib/libjbig2enc* /usr/local/lib/

# Install runtime dependencies only
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ghostscript \
        libleptonica-dev \
    && ldconfig \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080

CMD gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --workers 2 \
    --threads 4 \
    --timeout 600 \
    --keep-alive 65
