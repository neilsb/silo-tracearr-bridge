FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY silo_tracearr ./silo_tracearr
USER 65532:65532
EXPOSE 8097
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('BRIDGE_PORT','8097')+'/health/live',timeout=3)" || exit 1
CMD ["python", "-m", "silo_tracearr"]
