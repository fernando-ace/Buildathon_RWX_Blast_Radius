# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN python -m pip install --no-cache-dir orjson==3.13.0
COPY selector.py /app/selector.py
COPY impact_map.json /app/impact_map.json
RUN python -c "import orjson, pickle; from pathlib import Path; source = Path('/app/impact_map.json'); target = Path('/app/impact_map.pickle'); target.write_bytes(pickle.dumps(orjson.loads(source.read_bytes()), protocol=5))"
ENV BLAST_RADIUS_MAP=/app/impact_map.pickle
CMD ["python", "/app/selector.py"]
