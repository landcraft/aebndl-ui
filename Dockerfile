FROM python:3.11-slim

# Install system dependencies (ffmpeg is required, git/curl for update script/checkout if needed in debugging)
# We also need build-essential for some python packages if wheels aren't available, but slim usually needs it.
# However, aebn_dl depends on curl_cffi which usually distributes binary wheels.
# lxml also usually has wheels.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirement first for caching
COPY src/requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Environment Variables
ENV DOWNLOAD_DIR=/downloads
ENV PYTHONPATH=/app/source

# Create download directory
RUN mkdir -p /downloads

# Expose port
EXPOSE 21345

# Command to run the app
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "21345"]
