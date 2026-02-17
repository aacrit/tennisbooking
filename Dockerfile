FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium only for smaller image)
RUN playwright install chromium

# Copy application
COPY . .

# Create data directory for SQLite
RUN mkdir -p data

EXPOSE 8080

CMD ["python", "main.py"]
