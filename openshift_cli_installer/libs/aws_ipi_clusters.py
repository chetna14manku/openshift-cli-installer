import functools
import json
import os
import shlex

import click
import yaml
from jinja2 import DebugUndefined, Environment, FileSystemLoader, meta
from ocp_utilities.utils import run_command

from openshift_cli_installer.utils.cluster_versions import set_clusters_versions
from openshift_cli_installer.utils.const import CREATE_STR, DESTROY_STR
from openshift_cli_installer.utils.helpers import (
    bucket_object_name,
    cluster_shortuuid,
    dump_cluster_data_to_file,
    get_manifests_path,
    zip_and_upload_to_s3,
)

# TODO: enable spot
"""
function inject_spot_instance_config() {
  local dir=${1}

  if [ ! -f /tmp/yq ]; then
    curl -L https://github.com/mikefarah/yq/releases/download/3.3.0/yq_linux_amd64 -o /tmp/yq && chmod +x /tmp/yq
  fi

  PATCH="${SHARED_DIR}/machinesets-spot-instances.yaml.patch"
  cat > "${PATCH}" << EOF
spec:
  template:
    spec:
      providerSpec:
        value:
          spotMarketOptions: {}
EOF

  for MACHINESET in $dir/openshift/99_openshift-cluster-api_worker-machineset-*.yaml; do
    /tmp/yq m -x -i "${MACHINESET}" "${PATCH}"
    echo "Patched spotMarketOptions into ${MACHINESET}"
  done

  echo "Enabled AWS Spot instances for worker nodes"
}
"""


def prepare_pull_secret(clusters, pull_secret):
    for cluster in clusters:
        pull_secret_file = os.path.join(cluster["auth-dir"], "pull-secret.json")
        with open(pull_secret_file, "w") as fd:
            fd.write(json.dumps(pull_secret))

        cluster["registry_config"] = pull_secret
        cluster["pull-secret-file"] = pull_secret_file


def create_install_config_file(clusters, registry_config_file, ssh_key_file):
    pull_secret = json.dumps(
        get_pull_secret_data(registry_config_file=registry_config_file)
    )
    for _cluster in clusters:
        install_dir = _cluster["install-dir"]
        _cluster["ssh_key"] = get_local_ssh_key(ssh_key_file=ssh_key_file)
        _cluster["pull_secret"] = pull_secret
        cluster_install_config = get_install_config_j2_template(cluster_dict=_cluster)

        with open(os.path.join(install_dir, "install-config.yaml"), "w") as fd:
            fd.write(yaml.dump(cluster_install_config))

    return clusters


def get_pull_secret_data(registry_config_file):
    with open(registry_config_file) as fd:
        return json.load(fd)


def get_local_ssh_key(ssh_key_file):
    with open(ssh_key_file) as fd:
        return fd.read().strip()


def get_install_config_j2_template(cluster_dict):
    env = Environment(
        loader=FileSystemLoader(get_manifests_path()),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=DebugUndefined,
    )

    template = env.get_template(name="install-config-template.j2")
    rendered = template.render(cluster_dict)
    undefined_variables = meta.find_undeclared_variables(env.parse(rendered))
    if undefined_variables:
        click.secho(
            f"The following variables are undefined: {undefined_variables}", fg="red"
        )
        raise click.Abort()

    return yaml.safe_load(rendered)


def download_openshift_install_binary(clusters, registry_config_file):
    versions_urls = set()
    openshift_install_str = "openshift-install"

    for cluster in clusters:
        versions_urls.add(f"{cluster['version_url']}:{cluster['version']}")

    for version_url in versions_urls:
        binary_dir = os.path.join("/tmp", version_url)
        for cluster in clusters:
            if version_url.endswith(cluster["version"]):
                cluster["openshift-install-binary"] = os.path.join(
                    binary_dir, openshift_install_str
                )

        rc, _, err = run_command(
            command=shlex.split(
                "oc adm release extract "
                f"{version_url} "
                f"--command={openshift_install_str} --to={binary_dir} --registry-config={registry_config_file}"
            ),
            check=False,
        )
        if not rc:
            click.secho(
                f"Failed to get {openshift_install_str} for version {version_url}, error: {err}",
                fg="red",
            )
            raise click.Abort()

    return clusters


def create_or_destroy_aws_ipi_cluster(
    cluster_data, action, s3_bucket_name=None, s3_bucket_path=None, cleanup=False
):
    install_dir = cluster_data["install-dir"]
    binary_path = cluster_data["openshift-install-binary"]
    res, out, err = run_command(
        command=shlex.split(f"{binary_path} {action} cluster --dir {install_dir}"),
        capture_output=False,
        check=False,
    )

    if action == CREATE_STR:
        _shortuuid = cluster_shortuuid()
        cluster_data["s3_object_name"] = bucket_object_name(
            cluster_data=cluster_data,
            _shortuuid=_shortuuid,
            s3_bucket_path=s3_bucket_path,
        )
        dump_cluster_data_to_file(cluster_data=cluster_data)

        if s3_bucket_name:
            zip_and_upload_to_s3(
                install_dir=install_dir,
                s3_bucket_name=s3_bucket_name,
                s3_bucket_path=s3_bucket_path,
                uuid=_shortuuid,
            )

    if not res:
        if not cleanup:
            click.secho(
                f"Failed to run cluster {action}\n\tERR: {err}\n\tOUT: {out}.", fg="red"
            )
            if action == CREATE_STR:
                click.echo("Cleaning leftovers.")
                create_or_destroy_aws_ipi_cluster(
                    cluster_data=cluster_data, action=DESTROY_STR, cleanup=True
                )

        raise click.Abort()


@functools.cache
def get_aws_versions(docker_config_json_dir_path=None):
    # If running on openshift-ci we need to set `DOCKER_CONFIG`
    if os.environ.get("OPENSHIFT_CI") == "true":
        click.echo("Running in openshift ci")
        os.environ["DOCKER_CONFIG"] = docker_config_json_dir_path

    versions_dict = {}
    for source_repo in [
        "quay.io/openshift-release-dev/ocp-release",
        "registry.ci.openshift.org/ocp/release",
    ]:
        versions_dict[source_repo] = run_command(
            command=shlex.split(f"regctl tag ls {source_repo}"),
            check=False,
        )[1].splitlines()

    return versions_dict


def update_aws_clusters_versions(
    clusters, docker_config_json_dir_path=None, _test=False
):
    for _cluster_data in clusters:
        _cluster_data["stream"] = _cluster_data.get("stream", "stable")

    base_available_versions = get_all_versions(
        docker_config_json_dir_path=docker_config_json_dir_path, _test=_test
    )

    return set_clusters_versions(
        clusters=clusters,
        base_available_versions=base_available_versions,
    )


def get_all_versions(docker_config_json_dir_path=None, _test=None):
    if _test:
        with open("openshift_cli_installer/tests/all_aws_versions.json") as fd:
            base_available_versions = json.load(fd)
    else:
        base_available_versions = get_aws_versions(
            docker_config_json_dir_path=docker_config_json_dir_path
        )

    return base_available_versions