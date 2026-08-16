# Use an official, lightweight Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# ADDED 'git' to the install list
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev git && \
    rm -rf /var/lib/apt/lists/*

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The command to start the application
CMD ["python", "app.py"]
