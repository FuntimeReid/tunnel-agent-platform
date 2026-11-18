FROM infiniflow/ragflow:v0.27.2
COPY patches/ /ragflow/
COPY api_entry.py /ragflow/retrieval_api_entry.py
WORKDIR /ragflow
ENTRYPOINT ["/ragflow/.venv/bin/python", "/ragflow/retrieval_api_entry.py"]
