import contextlib
import copy
import os
import shlex
import shutil
from pathlib import Path

import click
import rosa.cli
import shortuuid
import yaml
from ocm_python_wrapper.ocm_client import OCMPythonClient
from ocm_python_wrapper.versions import Versions
from ocp_resources.route import Route
from ocp_utilities.infra import get_client
from ocp_utilities.must_gather import run_must_gather
from ocp_utilities.utils import run_command

from openshift_cli_installer.utils.cluster_versions import set_clusters_versions
from openshift_cli_installer.utils.const import (
    AWS_OSD_STR,
    CLUSTER_DATA_YAML_FILENAME,
    ERROR_LOG_COLOR,
    GCP_OSD_STR,
    HYPERSHIFT_STR,
    ROSA_STR,
    WARNING_LOG_COLOR,
)
from openshift_cli_installer.utils.general import bucket_object_name


def get_ocm_client(ocm_token, ocm_env):
    return OCMPythonClient(
        token=ocm_token,
        endpoint="https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token",
        api_host=ocm_env,
        discard_unknown_keys=True,
    ).client


def dump_cluster_data_to_file(cluster_data):
    _cluster_data = copy.copy(cluster_data)
    _cluster_data.pop("ocm-client", "")
    _cluster_data.pop("timeout-watch", "")
    _cluster_data.pop("ocp-client", "")
    _cluster_data.pop("cluster-object", "")
    with open(
        os.path.join(_cluster_data["install-dir"], CLUSTER_DATA_YAML_FILENAME), "w"
    ) as fd:
        fd.write(yaml.dump(_cluster_data))


def update_rosa_osd_clusters_versions(clusters, _test=False, _test_versions_dict=None):
    if _test:
        base_available_versions_dict = _test_versions_dict
    else:
        base_available_versions_dict = {}
        for cluster_data in clusters:
            if cluster_data["platform"] in (AWS_OSD_STR, GCP_OSD_STR):
                base_available_versions_dict.update(
                    Versions(client=cluster_data["ocm-client"]).get(
                        channel_group=cluster_data["channel-group"]
                    )
                )

            elif cluster_data["platform"] in (ROSA_STR, HYPERSHIFT_STR):
                channel_group = cluster_data["channel-group"]
                base_available_versions = rosa.cli.execute(
                    command=(
                        f"list versions --channel-group={channel_group} "
                        f"{'--hosted-cp' if cluster_data['platform'] == HYPERSHIFT_STR else ''}"
                    ),
                    aws_region=cluster_data["region"],
                    ocm_client=cluster_data["ocm-client"],
                )["out"]
                _all_versions = [ver["raw_id"] for ver in base_available_versions]
                base_available_versions_dict.setdefault(channel_group, []).extend(
                    _all_versions
                )

    return set_clusters_versions(
        clusters=clusters,
        base_available_versions=base_available_versions_dict,
    )


def add_cluster_info_to_cluster_data(cluster_data):
    """
    Adds cluster information to the given clusters data dictionary.

    `cluster-id`, `api-url` and `console-url` (when available) will be added to `cluster_data`.

    Args:
        cluster_data (dict): A dictionary containing cluster data.

    Returns:
        dict: The updated cluster data dictionary.
    """
    if cluster_data["platform"] == HYPERSHIFT_STR:
        click.secho(
            f"{HYPERSHIFT_STR} clusters do not have console URL", fg=WARNING_LOG_COLOR
        )

    cluster_object = cluster_data.get("cluster-object")
    if cluster_object:
        ocp_client = cluster_object.ocp_client
        cluster_data["cluster-id"] = cluster_object.cluster_id

    else:
        ocp_client = get_client(config_file=f"{cluster_data['auth-dir']}/kubeconfig")

    cluster_data["ocp-client"] = ocp_client
    cluster_data["api-url"] = ocp_client.configuration.host
    console_route = Route(
        name="console", namespace="openshift-console", client=ocp_client
    )
    if console_route.exists:
        route_spec = console_route.instance.spec
        cluster_data["console-url"] = (
            f"{route_spec.port.targetPort}://{route_spec.host}"
        )

    return cluster_data


def set_cluster_auth(cluster_data):
    auth_path = os.path.join(cluster_data["install-dir"], "auth")
    Path(auth_path).mkdir(parents=True, exist_ok=True)

    cluster_object = cluster_data["cluster-object"]
    with open(os.path.join(auth_path, "kubeconfig"), "w") as fd:
        fd.write(yaml.dump(cluster_object.kubeconfig))

    with open(os.path.join(auth_path, "kubeadmin-password"), "w") as fd:
        fd.write(cluster_object.kubeadmin_password)


def check_ocm_managed_existing_clusters(clusters):
    if clusters:
        click.echo("Check for existing OCM-managed clusters.")
        existing_clusters_list = []
        for _cluster in clusters:
            cluster_name = _cluster["name"]
            if _cluster["cluster-object"].exists:
                existing_clusters_list.append(cluster_name)

        if existing_clusters_list:
            click.secho(
                f"At least one cluster already exists: {existing_clusters_list}",
                fg=ERROR_LOG_COLOR,
            )
            raise click.Abort()


def add_s3_bucket_data(clusters, s3_bucket_name, s3_bucket_path=None):
    for cluster in clusters:
        cluster["shortuuid"] = shortuuid.uuid()
        cluster["s3-bucket-name"] = s3_bucket_name
        cluster["s3-bucket-path"] = s3_bucket_path
        cluster["s3-object-name"] = bucket_object_name(
            cluster_data=cluster, s3_bucket_path=s3_bucket_path
        )

    return clusters


def collect_must_gather(must_gather_output_dir, cluster_data):
    try:
        name = cluster_data["name"]
        platform = cluster_data["platform"]
        target_dir = os.path.join(must_gather_output_dir, "must-gather", platform, name)
    except Exception as ex:
        click.secho(
            f"Failed to get data from {cluster_data}; must-gather could not be executed"
            f" on: {ex}",
            fg=ERROR_LOG_COLOR,
        )
        return

    try:
        kubeconfig_path = get_kubeconfig_path(cluster_data=cluster_data)
        if not os.path.exists(kubeconfig_path):
            click.secho(
                f"kubeconfig for cluster {name} does not exist; cannot run"
                " must-gather.",
                fg=ERROR_LOG_COLOR,
            )
            return

        click.echo(f"Prepare must-gather target extracted directory {target_dir}.")
        Path(target_dir).mkdir(parents=True, exist_ok=True)

        click.echo(f"Collect must-gather for cluster {name} running on {platform}")
        run_must_gather(
            target_base_dir=target_dir,
            kubeconfig=kubeconfig_path,
        )

    except Exception as ex:
        click.secho(
            f"Failed to run must-gather for cluster {name} on"
            f" {platform} platform\n{ex}",
            fg=ERROR_LOG_COLOR,
        )

        click.echo(f"Delete must-gather target directory {target_dir}.")
        shutil.rmtree(target_dir)


def get_kubeconfig_path(cluster_data):
    return os.path.join(cluster_data["auth-dir"], "kubeconfig")


@contextlib.contextmanager
def get_kubeadmin_token(cluster_data):
    with open(
        os.path.join(cluster_data["install-dir"], "auth", "kubeadmin-password")
    ) as fd:
        kubeadmin_password = fd.read()
    run_command(
        shlex.split(
            f"oc login {cluster_data['api-url']} -u kubeadmin -p {kubeadmin_password}"
        ),
        hide_log_command=True,
    )
    yield run_command(
        shlex.split("oc whoami -t"),
        hide_log_command=True,
    )[1].strip()
    run_command(shlex.split("oc logout"))
