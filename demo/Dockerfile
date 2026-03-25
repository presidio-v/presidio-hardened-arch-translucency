FROM python:3.12-slim
LABEL maintainer="presidio-v" \
      description="Monte Carlo π workload server for pat demo"
WORKDIR /app
COPY app.py .
EXPOSE 8080
CMD ["python", "-u", "app.py"]
