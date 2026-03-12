FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ src/
COPY config/ config/

# Non-root user for security
RUN useradd -r -s /bin/false agent
USER agent

ENTRYPOINT ["python", "-m", "src.main"]
