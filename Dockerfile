FROM python:alpine
RUN apk upgrade && apk add --no-cache bash gcc libffi-dev musl-dev openssl-dev python3-dev curl
RUN mkdir /app
WORKDIR /app
COPY . /app
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
RUN pip install -r requirements.txt
CMD ["python", "main.py"]
