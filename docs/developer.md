# Developer documentation

## Requirements

- Docker with Compose plugin "docker compose"

## Launch local deployment

If you wish to override any of the environment variables, create a `.env` file in the `/docker` directory:

```bash
echo 'DEV_MODE = true' >> docker/.env
```

Launch the local deployment by navigating to the root directory of your repo clone and executing:

```bash
docker compose -f docker/docker-compose.yaml up -d --build
```

## Testing

## Virtual environment

You may want to use a Python virtual environment when developing code or when testing. In general it is possible to `docker exec` into a running container where the environment is already configured, but sometimes it is convenient to run a script on your host machine. In these cases, you can create a virtual environment and install the requirements locally.

```bash
# Create the venv once
$ python -m venv .venv
# Thereafter, source the activate script prior to running Python scripts that require the project dependencies
$ source .venv/bin/activate
(.venv) 
# Install/update all dependencies 
$ pip install -r crossmatch/requirements.base.txt 
```

## Unit tests

Run the unit tests using commands similar to the one below after launching the dev deployment.

```bash
docker compose -f docker/docker-compose.yaml up -d --build 
docker exec -it crossmatch-api-server-1 bash -c 'python manage.py test crossmatch.tests.crossmatch'
```
