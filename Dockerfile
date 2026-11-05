FROM python:3.12-slim
WORKDIR /srv
COPY . .
RUN pip install --no-cache-dir .
ENV DATACLEAN_TRANSPORT=http PORT=8080
EXPOSE 8080
USER nobody
CMD ["dataclean-mcp", "--http"]
