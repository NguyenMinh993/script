# Use an official Python runtime as the base image
FROM python:3.9-slim

# Set working directory inside the container
WORKDIR /app

# Install system dependencies (including FFmpeg and Git)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Whisper separately (if not already in requirements.txt)
RUN pip install git+https://github.com/openai/whisper.git

# Copy the FastAPI application code
COPY server.py .

# Create temp and output directories
RUN mkdir -p temp output

# Expose the port FastAPI will run on
EXPOSE 8000

# Command to run the FastAPI app with uvicorn
CMD ["uvicorn", "server.py:server", "--host", "0.0.0.0", "--port", "8000"]
