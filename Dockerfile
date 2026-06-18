FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ghostscript \
        jbig2dec \
        build-essential \
        automake \
        autoconf \
        libtool \
        libleptonica-dev \
        zlib1g-dev \
        libpng-dev \
        git \
    && git clone https://github.com/agl/jbig2enc.git /tmp/jbig2enc \
    && cd /tmp/jbig2enc \
    && ./autogen.sh \
    && ./configure \
    && make \
    && make install \
    && ldconfig \
    && cd / \
    && rm -rf /tmp/jbig2enc \
    && apt-get purge -y build-essential automake autoconf libtool git \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway sets PORT env var; default to 8080
ENV PORT=8080

CMD gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --workers 2 \
    --threads 4 \
    --timeout 600 \
    --keep-alive 65
