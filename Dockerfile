FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 4534
CMD ["python", "main.py"]

#ENV FLASK_APP=main.py
#CMD ["flask", "run", "--host", "0.0.0.0"]
