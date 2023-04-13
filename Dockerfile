FROM python:alpine
RUN apk upgrade && apk add --no-cache bash gcc libffi-dev musl-dev openssl-dev python3-dev
RUN mkdir /app
WORKDIR /app
COPY . /app
RUN pip install -r requirements.txt
CMD ["python", "main.py"]
