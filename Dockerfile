FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependencies install from the manifest alone, so editing application code
# does not invalidate this layer. Pass --build-arg EXTRAS=dev to add the dev
# dependency group.
ARG EXTRAS=""
COPY pyproject.toml ./
RUN python - "$EXTRAS" > /tmp/requirements.txt <<'PY' \
 && pip install --no-cache-dir -r /tmp/requirements.txt
import sys
import tomllib

with open("pyproject.toml", "rb") as handle:
    project = tomllib.load(handle)["project"]

requirements = list(project["dependencies"])
for group in filter(None, sys.argv[1].split(",")):
    requirements += project["optional-dependencies"][group]

print("\n".join(requirements))
PY

COPY src ./src
COPY prompts ./prompts

EXPOSE 8000

CMD ["python", "-m", "app.main"]
