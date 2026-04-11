# Use official Python runtime as a parent image, matching your runtime.txt
FROM python:3.13-slim

# Set environment variables
# Prevents Python from writing pyc files to disc
ENV PYTHONDONTWRITEBYTECODE=1
# Prevents Python from buffering stdout and stderr
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies (useful for python packages that need to be compiled)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements file first to leverage Docker cache
COPY requirements.txt /app/

# Install dependencies
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . /app/

# Expose the port that Gunicorn will run on
EXPOSE 8000

# Run the application using Gunicorn (matches your Procfile)
CMD ["gunicorn", "run:app", "--workers", "2", "--threads", "2", "--timeout", "120", "--bind", "0.0.0.0:8000"]
