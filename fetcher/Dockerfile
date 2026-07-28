# Playwright's own image already has Chromium plus every system library it
# needs -- the ones that had to be apt-installed by hand to make this work
# locally. That bundle is why this Lambda ships as a container image and
# not a zip: it is far past Lambda's 250MB unzipped limit.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

# A container only becomes a Lambda by speaking Lambda's runtime protocol.
# awslambdaric (Runtime Interface Client) is the adapter that does that,
# turning `handler.lambda_handler` into something Lambda can invoke.
RUN pip install --no-cache-dir awslambdaric

WORKDIR /var/task
COPY handler.py .

# Chromium writes profile and cache data; Lambda only gives us /tmp.
ENV HOME=/tmp
ENV XDG_CACHE_HOME=/tmp/.cache

ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["handler.lambda_handler"]
