# Step 1: Base image with Python
FROM python:3.11-slim

# Step 2: Install system dependencies (for ansible + ssh + other tools)
RUN apt-get update && apt-get install -y \
    ansible \
    sshpass \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Step 3: Create app directory
WORKDIR /app

# Step 4: Copy project files
COPY . /app

# Step 5: Install Python dependencies (requirements.txt + ansible-runner)
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install ansible-runner

# Step 6: Prepare kubeconfig directories (for mounting at runtime)
RUN mkdir -p /root/.kube /etc/kubernetes

# Step 7: Set default command
CMD ["python", "main.py"]
