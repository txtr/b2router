# Build stage
FROM python:3.11.10-slim AS builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Runtime stage
FROM python:3.11.10-slim

WORKDIR /app

# Create non-root user
RUN useradd --no-create-home --shell /bin/bash --uid 1000 appuser

# Copy installed packages from builder
COPY --from=builder /root/.local /home/appuser/.local

# Copy application
COPY b2.py .
COPY b2router/ ./b2router/
COPY test_bugs.py .
COPY accounts.yaml.example .

# Set ownership
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Add local packages to PATH
ENV PATH=/home/appuser/.local/bin:$PATH

# Default command
ENTRYPOINT ["python", "b2.py"]
CMD ["--help"]