FROM python:alpine3.17

WORKDIR /app

RUN pip install --upgrade pip==25.0.1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH="/app"

CMD ["python", "-m", "src.main"]
