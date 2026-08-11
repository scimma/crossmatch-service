FROM python:3.12.12 AS deps

# Install from the fully-pinned lock (compiled from requirements.base.txt by
# pip-compile) so the image's transitive dependency set is reproducible — this
# is what guarantees hats==0.10.4 et al. across rebuilds. Regenerate the lock
# with: pip-compile --strip-extras --output-file=requirements.lock requirements.base.txt
COPY ./requirements.lock /requirements.txt
RUN pip install -r /requirements.txt --no-cache-dir

FROM python:3.12.12

ARG UID=1000
ARG USERNAME=crossmatch
ARG APP_ROOT_DIR=/opt

RUN useradd --create-home --shell /bin/bash ${USERNAME} --uid ${UID}

COPY --from=deps /usr/local/bin/ /usr/local/bin/
COPY --from=deps /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/

## Create application base directory
RUN mkdir -p ${APP_ROOT_DIR} && chown ${UID} -R ${APP_ROOT_DIR}
USER ${USERNAME}

WORKDIR ${APP_ROOT_DIR}
COPY --chown=${USERNAME}:${USERNAME} . ./crossmatch
WORKDIR ${APP_ROOT_DIR}/crossmatch
