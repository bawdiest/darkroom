FROM python:3.12-slim
# bubblewrap: Sandbox fuer Scripts (siehe README)
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY darkroom ./darkroom
ENV DARKROOM_LIBRARY=/library DARKROOM_DATA=/data
EXPOSE 8000
CMD ["uvicorn", "darkroom.app:app", "--host", "0.0.0.0", "--port", "8000"]
