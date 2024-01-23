import datetime
import os
import shutil
import time

import click
from simple_logger.logger import get_logger

from openshift_cli_installer.libs.clusters.ocp_clusters import OCPClusters
from openshift_cli_installer.libs.user_input import UserInput
from openshift_cli_installer.utils.click_dict_type import DictParamType
from openshift_cli_installer.utils.clusters import (
    destroy_clusters_from_s3_bucket_or_local_directory,
)
from openshift_cli_installer.utils.const import (
    CREATE_STR,
    DESTROY_CLUSTERS_FROM_S3_BASE_DATA_DIRECTORY,
    DESTROY_STR,
)
from openshift_cli_installer.utils.gcp_utils import set_gcp_configuration, restore_gcp_configuration


@click.command("installer")
@click.option(
    "-a",
    "--action",
    type=click.Choice([CREATE_STR, DESTROY_STR]),
    help="Action to perform Openshift cluster/s",
)
@click.option(
    "-p",
    "--parallel",
    help="Run clusters install/uninstall in parallel",
    is_flag=True,
    show_default=True,
)
@click.option(
    "--ssh-key-file",
    help="id_rsa.pub file path for AWS IPI or ACM clusters",
    default="/openshift-cli-installer/ssh-key/id_rsa.pub",
    type=click.Path(),
    show_default=True,
)
@click.option(
    "--clusters-install-data-directory",
    help="""
\b
Path to clusters install data.
    For install this will be used to store the install data.
    For uninstall this will be used to uninstall the clusters.
    Also used to store clusters kubeconfig.
    Default: "/openshift-cli-installer/clusters-install-data"
""",
    default=os.environ.get("CLUSTER_INSTALL_DATA_DIRECTORY"),
    type=click.Path(),
    show_default=True,
)
@click.option(
    "--registry-config-file",
    help="""
    \b
registry-config file, can be obtained from https://console.redhat.com/openshift/create/local.
(Needed only for AWS IPI clusters)
    """,
    default=os.environ.get("PULL_SECRET"),
    type=click.Path(),
    show_default=True,
)
@click.option(
    "--docker-config-file",
    type=click.Path(),
    default=os.path.expanduser("~/.docker/config.json"),
    help="""
    \b
Path to Docker config.json file.
File must include token for `registry.ci.openshift.org`
(Needed only for AWS IPI clusters)
    """,
)
@click.option(
    "--s3-bucket-name",
    help="S3 bucket name to store install folder backups",
    show_default=True,
)
@click.option("--s3-bucket-path", help="S3 bucket path to store the backups", show_default=True, default="")
@click.option(
    "--s3-bucket-path-uuid",
    help="S3 bucket path UUID to append to the S3 zip file name",
    show_default=True,
)
@click.option(
    "--ocm-token",
    help="OCM token.",
    default=os.environ.get("OCM_TOKEN"),
)
@click.option(
    "--aws-access-key-id",
    help="AWS access-key-id, needed for OSD AWS clusters.",
    default=os.environ.get("AWS_ACCESS_KEY_ID"),
)
@click.option(
    "--aws-secret-access-key",
    help="AWS secret-access-key, needed for OSD AWS clusters.",
    default=os.environ.get("AWS_SECRET_ACCESS_KEY"),
)
@click.option(
    "--aws-account-id",
    help="AWS account-id, needed for OSD AWS and Hypershift clusters.",
    default=os.environ.get("AWS_ACCOUNT_ID"),
)
@click.option(
    "-c",
    "--cluster",
    type=DictParamType(),
    help="""
\b
Cluster/s to install.
Format to pass is:
    'name=cluster1;base-domain=aws.domain.com;platform=aws;region=us-east-2;version=4.14.0-ec.2'
Required parameters:
    name: Cluster name.
    base-domain: Base domain for the cluster.
    platform: Cloud platform to install the cluster on, supported platforms are: aws, rosa and hypershift.
    region: Region to use for the cloud platform.
    version: Openshift cluster version to install
\b
Check <aws/gcp>-install-config-template.j2 for variables that can be overwritten by the user.
For example:
    fips=true
    worker-flavor=m5.xlarge
    worker-replicas=6
    """,
    multiple=True,
)
@click.option(
    "--destroy-clusters-from-s3-bucket",
    help="""
\b
Destroy clusters from S3 bucket, --s3-bucket-name is required and optional --s3-bucket-path.
    """,
    show_default=True,
    is_flag=True,
)
@click.option(
    "--destroy-clusters-from-s3-bucket-query",
    help="""
\b
Destroy cluster(s) from S3 bucket which match only files that have it.
    """,
    show_default=True,
)
@click.option(
    "--destroy-clusters-from-install-data-directory",
    help="""
\b
Destroy clusters from cluster data files located at --clusters-install-data-directory
    """,
    show_default=True,
    is_flag=True,
)
@click.option(
    "--destroy-clusters-from-install-data-directory-using-s3-bucket",
    help="""
\b
Destroy clusters from cluster data files located at --clusters-install-data-directory
Get the S3 object from cluster_data.yaml, download, extract it and call destroy cluster
    """,
    show_default=True,
    is_flag=True,
)
@click.option(
    "--clusters-yaml-config-file",
    help="""
    \b
    YAML file with configuration to create clusters, any option in YAML file will override the CLI option.
    See manifests/clusters.example.yaml for example.
    """,
    type=click.Path(exists=True),
)
@click.option(
    "--gcp-service-account-file",
    help="""
\b
Path to GCP service account json file.
""",
    type=click.Path(exists=True),
)
@click.option(
    "--must-gather-output-dir",
    help="""
\b
Path to must-gather output directory.
must-gather will try to collect data when cluster installation fails and cluster can be accessed.
""",
    type=click.Path(exists=True),
)
@click.option(
    "--dry-run",
    help="For testing, only verify user input",
    is_flag=True,
    show_default=True,
)
@click.option(
    "--pdb",
    help="Drop to `ipdb` shell on exception",
    is_flag=True,
    show_default=True,
)
def main(**kwargs):
    """
    Create/Destroy Openshift cluster/s
    """
    kwargs.pop("pdb", None)
    user_input = UserInput(**kwargs)

    if kwargs["dry_run"]:
        return

    gcp_params = set_gcp_configuration(user_input=user_input)

    try:
        if (
            kwargs["destroy_clusters_from_s3_bucket"]
            or kwargs["destroy_clusters_from_install_data_directory"]
            or kwargs["destroy_clusters_from_install_data_directory_using_s3_bucket"]
            or kwargs["destroy_clusters_from_s3_bucket_query"]
        ):
            clusters_kwargs = {"destroy_from_s3_bucket_or_local_directory": True}
            clusters_kwargs.update(destroy_clusters_from_s3_bucket_or_local_directory(**kwargs))

            try:
                clusters = OCPClusters(**clusters_kwargs)
                clusters.run_create_or_destroy_clusters()
            finally:
                shutil.rmtree(DESTROY_CLUSTERS_FROM_S3_BASE_DATA_DIRECTORY, ignore_errors=True)

        else:
            clusters = OCPClusters(**kwargs)
            clusters.run_create_or_destroy_clusters()

            if kwargs["action"] == CREATE_STR:
                clusters.install_acm_on_clusters()
                clusters.enable_observability_on_acm_clusters()
                clusters.attach_clusters_to_acm_cluster_hub()

    finally:
        restore_gcp_configuration(gcp_params=gcp_params)


if __name__ == "__main__":
    start_time = time.time()
    should_raise = False
    _logger = get_logger(name="main-openshift-cli-installer")
    try:
        main()
    except Exception as ex:
        import sys
        import traceback

        ipdb = __import__("ipdb")  # Bypass debug-statements pre-commit hook

        if "--pdb" in sys.argv:
            extype, value, tb = sys.exc_info()
            traceback.print_exc()
            ipdb.post_mortem(tb)
        else:
            _logger.error(ex)
            should_raise = True
    finally:
        _logger.info(f"Total execution time: {datetime.timedelta(seconds=time.time() - start_time)}")
        if should_raise:
            sys.exit(1)
