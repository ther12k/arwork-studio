FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libcairo2 libglib2.0-0 fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV STUDIO_WORKSPACE=/data OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
EXPOSE 8765
CMD ["python","-m","uvicorn","studio.app:app","--host","0.0.0.0","--port","8765","--workers","1"]
