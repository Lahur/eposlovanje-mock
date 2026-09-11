FROM python:3.14-slim

WORKDIR /app

RUN pip install --no-cache-dir pipenv

COPY Pipfile Pipfile.lock ./
RUN pipenv install --system --ignore-pipfile

COPY main.py config.py proxy.py f1_web_mock.py ./

EXPOSE 8082

CMD ["python", "main.py"]
