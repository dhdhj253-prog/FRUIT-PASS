FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies if required (sqlite3 included with python)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create volume directory for persistent SQLite database storage
RUN mkdir -p /data
ENV DB_PATH=/data/fruit_pass.db

VOLUME ["/data"]

# Run the bot
CMD ["python", "-m", "fruit_pass.bot_app"]
