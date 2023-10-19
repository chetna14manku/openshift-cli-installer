import functools
import json
import os
import shlex

import click
import yaml
from jinja2 import DebugUndefined, Environment, FileSystemLoader, meta
from ocp_utilities.utils import run_command

from openshift_cli_installer.utils.cluster_versions import set_clusters_versions
from openshift_cli_installer.utils.clusters import (
    add_cluster_info_to_cluster_data,
    collect_must_gather,
    dump_cluster_data_to_file,
)
from openshift_cli_installer.utils.const import (
    AWS_STR,
    CREATE_STR,
    DESTROY_STR,
    ERROR_LOG_COLOR,
    SUCCESS_LOG_COLOR,
)
from openshift_cli_installer.utils.general import (
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


def generate_unified_pull_secret(registry_config_file, docker_config_file):
    registry_config = get_pull_secret_data(registry_config_file=registry_config_file)
    docker_config = get_pull_secret_data(registry_config_file=docker_config_file)
    docker_config["auths"].update(registry_config["auths"])

    return json.dumps(docker_config)


def create_install_config_file(
    clusters, registry_config_file, ssh_key_file, docker_config_file
):
    pull_secret = generate_unified_pull_secret(
        registry_config_file=registry_config_file, docker_config_file=docker_config_file
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
            f"The following variables are undefined: {undefined_variables}",
            fg=ERROR_LOG_COLOR,
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
                f"Failed to get {openshift_install_str} for version {version_url},"
                f" error: {err}",
                fg=ERROR_LOG_COLOR,
            )
            raise click.Abort()

    return clusters


def aws_ipi_create_cluster(cluster_data, must_gather_output_dir=None):
    res, _, _ = run_aws_installer_command(
        cluster_data=cluster_data, action=CREATE_STR, raise_on_failure=False
    )
    name = cluster_data["name"]

    if res:
        cluster_data = add_cluster_info_to_cluster_data(
            cluster_data=cluster_data,
        )
        dump_cluster_data_to_file(cluster_data=cluster_data)

        click.secho(f"Cluster {name} created successfully", fg=SUCCESS_LOG_COLOR)

    s3_bucket_name = cluster_data.get("s3-bucket-name")
    if s3_bucket_name:
        zip_and_upload_to_s3(
            install_dir=cluster_data["install-dir"],
            s3_bucket_name=s3_bucket_name,
            s3_bucket_path=cluster_data["s3-bucket-path"],
            uuid=cluster_data["shortuuid"],
        )

    if not res:
        if must_gather_output_dir:
            collect_must_gather(
                must_gather_output_dir=must_gather_output_dir, cluster_data=cluster_data
            )

        click.echo(f"Cleaning {AWS_STR} cluster {name} leftovers.")
        aws_ipi_destroy_cluster(
            cluster_data=cluster_data,
        )

        raise click.Abort()

    return cluster_data


def aws_ipi_destroy_cluster(
    cluster_data,
):
    run_aws_installer_command(
        cluster_data=cluster_data, action=DESTROY_STR, raise_on_failure=True
    )

    click.secho(
        f"Cluster {cluster_data['name']} destroyed successfully",
        fg=SUCCESS_LOG_COLOR,
    )
    return cluster_data


@functools.cache
def get_aws_versions():
    versions_dict = {}
    for source_repo in (
        "quay.io/openshift-release-dev/ocp-release",
        "registry.ci.openshift.org/ocp/release",
    ):
        versions_dict[source_repo] = run_command(
            command=shlex.split(f"regctl tag ls {source_repo}"),
            check=False,
        )[1].splitlines()

    return versions_dict


def update_aws_clusters_versions(clusters, _test=False):
    for _cluster_data in clusters:
        _cluster_data["stream"] = _cluster_data.get("stream", "stable")

    base_available_versions = get_all_versions(_test=_test)

    return set_clusters_versions(
        clusters=clusters,
        base_available_versions=base_available_versions,
    )


def get_all_versions(_test=None):
    if _test:
        with open("openshift_cli_installer/tests/all_aws_versions.json") as fd:
            base_available_versions = json.load(fd)
    else:
        base_available_versions = get_aws_versions()

    return base_available_versions


def run_aws_installer_command(cluster_data, action, raise_on_failure):
    res, out, err = run_command(
        command=shlex.split(
            f"{cluster_data['openshift-install-binary']} {action} cluster --dir"
            f" {cluster_data['install-dir']}"
            f" --log-level {cluster_data.get('log_level', 'error')}"
        ),
        capture_output=False,
        check=False,
    )

    if not res:
        click.secho(
            f"Failed to run cluster {action} for cluster {cluster_data['name']}\n\tERR:"
            f" {err}\n\tOUT: {out}.",
            fg=ERROR_LOG_COLOR,
        )
        if raise_on_failure:
            raise click.Abort()

    return res, out, err
