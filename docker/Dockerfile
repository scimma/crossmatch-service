FROM python:3.13 AS deps

COPY ./requirements.base.txt /requirements.txt
RUN pip install -r /requirements.txt --no-cache-dir

FROM python:3.13

ARG UID=1000
ARG USERNAME=crossmatch
ARG APP_ROOT_DIR=/opt

RUN useradd --create-home --shell /bin/bash ${USERNAME} --uid ${UID}

COPY --from=deps /usr/local/bin/ /usr/local/bin/
COPY --from=deps /usr/local/lib/python3.13/site-packages/ /usr/local/lib/python3.13/site-packages/

## Create application base directory
RUN mkdir -p ${APP_ROOT_DIR} && chown ${UID} -R ${APP_ROOT_DIR}
USER ${USERNAME}

WORKDIR ${APP_ROOT_DIR}
COPY --chown=${USERNAME}:${USERNAME} . ./crossmatch
WORKDIR ${APP_ROOT_DIR}/crossmatch
