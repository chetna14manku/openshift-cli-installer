import json
import os
import re
import shutil
from functools import wraps
from importlib.util import find_spec
from time import sleep

import click
import yaml
from clouds.aws.session_clients import s3_client
from jinja2 import DebugUndefined, Environment, FileSystemLoader, meta
from simple_logger.logger import get_logger

from openshift_cli_installer.utils.const import ERROR_LOG_COLOR

LOGGER = get_logger(name=__name__)


def remove_terraform_folder_from_install_dir(install_dir):
    """
    .terraform folder created when call terraform.init() and it's take more space.
    """
    folders_to_remove = []
    for root, dirs, files in os.walk(install_dir):
        for _dir in dirs:
            if _dir == ".terraform":
                folders_to_remove.append(os.path.join(root, _dir))

    for folder in folders_to_remove:
        shutil.rmtree(folder)


def ignore_exceptions(logger=None, retry=None):
    def wrapper(func):
        @wraps(func)
        def inner(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as ex:
                if retry:
                    for _ in range(0, retry):
                        try:
                            return func(*args, **kwargs)
                        except Exception:
                            sleep(1)

                if logger:
                    logger.info(ex)
                return None

        return inner

    return wrapper


@ignore_exceptions()
def zip_and_upload_to_s3(install_dir, s3_bucket_name, uuid, s3_bucket_path=None):
    remove_terraform_folder_from_install_dir(install_dir=install_dir)

    _base_name = f"{install_dir}-{uuid}"

    zip_file = shutil.make_archive(base_name=_base_name, format="zip", root_dir=install_dir)
    bucket_key = os.path.join(s3_bucket_path or "", os.path.split(zip_file)[-1])
    click.echo(f"Upload {zip_file} file to S3 {s3_bucket_name}, path {bucket_key}")
    s3_client().upload_file(Filename=zip_file, Bucket=s3_bucket_name, Key=bucket_key)

    return _base_name


def get_manifests_path():
    manifests_path = os.path.join("openshift_cli_installer", "manifests")
    if not os.path.isdir(manifests_path):
        manifests_path = os.path.join(find_spec("openshift_cli_installer").submodule_search_locations[0], "manifests")
    return manifests_path


# TODO: Move to own repository.
def tts(ts):
    """
    Convert time string to seconds.

    Args:
        ts (str): time string to convert, can be and int followed by s/m/h
            if only numbers was sent return int(ts)

    Example:
        >>> tts(ts="1h")
        3600
        >>> tts(ts="3600")
        3600

    Returns:
        int: Time in seconds
    """
    try:
        time_and_unit = re.match(r"(?P<time>\d+)(?P<unit>\w)", str(ts)).groupdict()
    except AttributeError:
        return int(ts)

    _time = int(time_and_unit["time"])
    _unit = time_and_unit["unit"].lower()
    if _unit == "s":
        return _time
    elif _unit == "m":
        return _time * 60
    elif _unit == "h":
        return _time * 60 * 60
    else:
        return int(ts)


def get_install_config_j2_template(jinja_dict, platform):
    env = Environment(
        loader=FileSystemLoader(get_manifests_path()), trim_blocks=True, lstrip_blocks=True, undefined=DebugUndefined
    )

    template = env.get_template(name=f"{platform}-install-config-template.j2")
    rendered = template.render(jinja_dict)
    undefined_variables = meta.find_undeclared_variables(env.parse(rendered))
    if undefined_variables:
        click.secho(f"The following variables are undefined: {undefined_variables}", fg=ERROR_LOG_COLOR)
        raise click.Abort()

    return yaml.safe_load(rendered)


def generate_unified_pull_secret(registry_config_file, docker_config_file):
    registry_config = get_pull_secret_data(registry_config_file=registry_config_file)
    docker_config = get_pull_secret_data(registry_config_file=docker_config_file)
    docker_config["auths"].update(registry_config["auths"])

    return json.dumps(docker_config)


def get_pull_secret_data(registry_config_file):
    with open(registry_config_file) as fd:
        return json.load(fd)


def get_local_ssh_key(ssh_key_file):
    with open(ssh_key_file) as fd:
        return fd.read().strip()


def get_dict_from_json(gcp_service_account_file):
    with open(gcp_service_account_file) as fd:
        return json.loads(fd.read())
