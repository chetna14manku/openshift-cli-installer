import ast
import os

import click
from pyaml_env import parse_config
from simple_logger.logger import get_logger

from openshift_cli_installer.utils.cli_utils import (
    get_aws_credentials_for_acm_observability,
    get_cluster_data_by_name_from_clusters,
    get_managed_acm_clusters_from_user_input,
)
from openshift_cli_installer.utils.const import (
    AWS_OSD_STR,
    CREATE_STR,
    GCP_STR,
    GCP_OSD_STR,
    HYPERSHIFT_STR,
    OBSERVABILITY_SUPPORTED_STORAGE_TYPES,
    S3_STR,
    SUPPORTED_ACTIONS,
    SUPPORTED_PLATFORMS,
    USER_INPUT_CLUSTER_BOOLEAN_KEYS,
    IPI_BASED_PLATFORMS,
)


class UserInput:
    def __init__(self, **kwargs):
        self.logger = get_logger(name=self.__class__.__module__)
        self.user_kwargs = kwargs
        self.clusters_yaml_config_file = self.user_kwargs.get("clusters_yaml_config_file")
        if self.clusters_yaml_config_file:
            # Update CLI user input from YAML file if exists
            # Since CLI user input has some defaults, YAML file will override them
            self.user_kwargs.update(parse_config(path=self.clusters_yaml_config_file, default_value=""))

        self.dry_run = self.user_kwargs.get("dry_run")
        self.action = self.user_kwargs.get("action")
        self.aws_access_key_id = self.user_kwargs.get("aws_access_key_id")
        self.aws_secret_access_key = self.user_kwargs.get("aws_secret_access_key")
        self.aws_account_id = self.user_kwargs.get("aws_account_id")
        self.gcp_service_account_file = self.user_kwargs.get("gcp_service_account_file")
        self.clusters = self.get_clusters_from_user_input()
        self.ocm_token = self.user_kwargs.get("ocm_token")
        self.parallel = False if self.clusters and len(self.clusters) == 1 else self.user_kwargs.get("parallel")
        self.clusters_install_data_directory = self.user_kwargs.get(
            "clusters_install_data_directory",
            "/openshift-cli-installer/clusters-install-data",
        )
        self.destroy_clusters_from_s3_config_files = self.user_kwargs.get("destroy_clusters_from_s3_config_files")
        self.s3_bucket_name = self.user_kwargs.get("s3_bucket_name")
        self.s3_bucket_path = self.user_kwargs.get("s3_bucket_path")
        self.s3_bucket_path_uuid = self.user_kwargs.get("s3_bucket_path_uuid")
        self.destroy_clusters_from_s3_bucket = self.user_kwargs.get("destroy_clusters_from_s3_bucket")
        self.destroy_clusters_from_s3_bucket_query = self.user_kwargs.get("destroy_clusters_from_s3_bucket_query")
        self.destroy_clusters_from_install_data_directory = self.user_kwargs.get(
            "destroy_clusters_from_install_data_directory"
        )
        self.destroy_clusters_from_install_data_directory_using_s3_bucket = self.user_kwargs.get(
            "destroy_clusters_from_install_data_directory_using_s3_bucket"
        )
        self.registry_config_file = self.user_kwargs.get("registry_config_file")
        self.ssh_key_file = self.user_kwargs.get("ssh_key_file")
        self.docker_config_file = self.user_kwargs.get("docker_config_file")
        self.must_gather_output_dir = self.user_kwargs.get("must_gather_output_dir")
        self.create = self.action == CREATE_STR

        # We need to make sure that we don't process the same input twice
        self._already_processed = "__openshift_cli_installer_user_input_processed__"
        if globals().get(self._already_processed):
            self.logger.info("User Input already processed")
            return

        if not self.dry_run:
            globals()[self._already_processed] = True

        self.logger.info("Initializing User Input")
        self.verify_user_input()

    def get_clusters_from_user_input(self):
        # From CLI, we get `cluster`, from YAML file we get `clusters`
        clusters = self.user_kwargs.get("cluster", [])
        if not clusters:
            clusters = self.user_kwargs.get("clusters", [])

        for _cluster in clusters:
            (
                aws_access_key_id,
                aws_secret_access_key,
            ) = get_aws_credentials_for_acm_observability(
                cluster=_cluster,
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,
            )
            _cluster["aws-access-key-id"] = aws_access_key_id
            _cluster["aws-secret-access-key"] = aws_secret_access_key
            if self.gcp_service_account_file:
                _cluster["gcp-service-account-file"] = self.gcp_service_account_file

            for key in USER_INPUT_CLUSTER_BOOLEAN_KEYS:
                cluster_key_value = _cluster.get(key)
                if cluster_key_value and isinstance(cluster_key_value, str):
                    try:
                        _cluster[key] = ast.literal_eval(cluster_key_value)
                    except ValueError:
                        continue

        return clusters

    def verify_user_input(self):
        self.abort_no_ocm_token()

        if self.destroy_clusters_from_s3_bucket or self.destroy_clusters_from_s3_bucket_query:
            if not self.s3_bucket_name:
                self.logger.error(
                    "`--s3-bucket-name` must be provided when running with `--destroy-clusters-from-s3-bucket` or `--destroy-clusters-from-s3-bucket-query`",
                )
                raise click.Abort()

        elif (
            self.destroy_clusters_from_install_data_directory
            and self.destroy_clusters_from_install_data_directory_using_s3_bucket
        ):
            self.logger.error(
                "`--destroy-clusters-from-install-data-directory-using-s3-bucket` is"
                " not supported when running with"
                " `--destroy-clusters-from-install-data-directory`",
            )
            raise click.Abort()

        elif (
            self.destroy_clusters_from_install_data_directory
            or self.destroy_clusters_from_install_data_directory_using_s3_bucket
        ):
            return

        else:
            if not self.action:
                self.logger.error("'action' must be provided, supported actions: `{SUPPORTED_ACTIONS}`")

                raise click.Abort()

            if self.action not in SUPPORTED_ACTIONS:
                self.logger.error(f"'{self.action}' is not supported, supported actions: `{SUPPORTED_ACTIONS}`")

                raise click.Abort()

            if not self.clusters:
                self.logger.error("At least one '--cluster' option must be provided.")
                raise click.Abort()

            self.is_platform_supported()
            self.assert_unique_cluster_names()
            self.assert_managed_acm_clusters_user_input()
            self.assert_ipi_installer_user_input()
            self.assert_aws_osd_hypershift_user_input()
            self.assert_acm_clusters_user_input()
            self.assert_gcp_user_input()
            self.assert_boolean_values()
            self.assert_cluster_platform_support_observability()

    def abort_no_ocm_token(self):
        if not self.ocm_token:
            self.logger.error("--ocm-token is required for clusters")
            raise click.Abort()

    def is_platform_supported(self):
        unsupported_platforms = []
        missing_platforms = []
        for _cluster in self.clusters:
            _platform = _cluster.get("platform")
            if not _platform:
                missing_platforms.append(f"Cluster {_cluster['name']} is missing platform")

            elif _platform not in SUPPORTED_PLATFORMS:
                unsupported_platforms.append(f"Cluster {_cluster['name']} platform '{_platform}' is not" " supported.")

        if unsupported_platforms or missing_platforms:
            if unsupported_platforms:
                self.logger.error("\n".join(unsupported_platforms))
                raise click.Abort()

            if missing_platforms:
                self.logger.error("\n".join(missing_platforms))
                raise click.Abort()

    def assert_unique_cluster_names(self):
        if self.create:
            cluster_names = [cluster["name"] for cluster in self.clusters]
            if len(cluster_names) != len(set(cluster_names)):
                self.logger.error(f"Cluster names must be unique: clusters {cluster_names}")
                raise click.Abort()

    def assert_managed_acm_clusters_user_input(self):
        if self.create:
            for cluster in self.clusters:
                managed_acm_clusters = get_managed_acm_clusters_from_user_input(cluster=cluster)
                for managed_acm_cluster in managed_acm_clusters:
                    managed_acm_cluster_data = get_cluster_data_by_name_from_clusters(
                        name=managed_acm_cluster, clusters=self.clusters
                    )
                    if not managed_acm_cluster_data:
                        self.logger.error(f"Managed ACM clusters: Cluster {managed_acm_cluster} not" " found")
                        raise click.Abort()

    def assert_ipi_installer_user_input(self):
        if any([_cluster["platform"] in IPI_BASED_PLATFORMS for _cluster in self.clusters]):
            self.assert_ipi_installer_log_level_user_input()
            self.assert_registry_config_file_exists()
            self.assert_docker_config_file_exists()
            if self.create:
                self.assert_public_ssh_key_file_exists()

    def assert_docker_config_file_exists(self):
        if not self.docker_config_file or not os.path.exists(self.docker_config_file):
            self.logger.error(
                "Docker config file is required for IPI installations."
                f" {self.docker_config_file} file does not exist."
            )
            raise click.Abort()

    def assert_ipi_installer_log_level_user_input(self):
        supported_log_levels = ["debug", "info", "warn", "error"]
        unsupported_log_levels = []
        for _cluster in self.clusters:
            if _cluster["platform"] in IPI_BASED_PLATFORMS:
                log_level = _cluster.get("log_level", "error")
                if log_level not in supported_log_levels:
                    unsupported_log_levels.append(f"LogLevel {log_level} for cluster {_cluster['name']}")

        if unsupported_log_levels:
            self.logger.error(
                f"{unsupported_log_levels} not supported for openshift-installer cli."
                f" Supported options are {supported_log_levels}"
            )
            raise click.Abort()

    def assert_public_ssh_key_file_exists(self):
        if not self.ssh_key_file or not os.path.exists(self.ssh_key_file):
            self.logger.error(
                "SSH file is required for IPI cluster installations. {self.ssh_key_file} file does not exist.",
            )
            raise click.Abort()

    def assert_registry_config_file_exists(self):
        if not self.registry_config_file or not os.path.exists(self.registry_config_file):
            self.logger.error(
                "Registry config file is required for IPI cluster installations."
                f" {self.registry_config_file} file does not exist.",
            )
            raise click.Abort()

    def assert_aws_osd_hypershift_user_input(self):
        if any([_cluster["platform"] in (AWS_OSD_STR, HYPERSHIFT_STR) for _cluster in self.clusters]):
            self.assert_aws_credentials_exist()
            if not self.aws_account_id and self.create:
                self.logger.error(
                    "--aws-account-id required for AWS OSD or Hypershift installations.",
                )
                raise click.Abort()

    def assert_aws_credentials_exist(self):
        if not (self.aws_secret_access_key and self.aws_access_key_id):
            self.logger.error(
                "--aws-secret-access-key and aws-access-key-id required for AWS OSD OR ACM cluster installations.",
            )
            raise click.Abort()

    def assert_acm_clusters_user_input(self):
        acm_clusters = [_cluster for _cluster in self.clusters if _cluster.get("acm") is True]
        if acm_clusters and self.create:
            for _cluster in acm_clusters:
                cluster_platform = _cluster["platform"]
                if cluster_platform == HYPERSHIFT_STR:
                    self.logger.error(f"ACM not supported for {cluster_platform} clusters")
                    raise click.Abort()

    def assert_gcp_user_input(self):
        if (
            self.create
            and any([cluster["platform"] in (GCP_OSD_STR, GCP_STR) for cluster in self.clusters])
            and not self.gcp_service_account_file
        ):
            self.logger.error(
                f"`--gcp-service-account-file` option must be provided for {GCP_OSD_STR} and {GCP_STR} clusters",
            )
            raise click.Abort()

    def assert_boolean_values(self):
        if self.create:
            for cluster in self.clusters:
                non_bool_keys = [
                    cluster_data_key
                    for cluster_data_key, cluster_data_value in cluster.items()
                    if cluster_data_key in USER_INPUT_CLUSTER_BOOLEAN_KEYS and not isinstance(cluster_data_value, bool)
                ]
                if non_bool_keys:
                    self.logger.error(f"The following keys must be booleans: {non_bool_keys}")
                    raise click.Abort()

    def assert_cluster_platform_support_observability(self):
        not_supported_clusters = []
        missing_storage_data = []
        for cluster in self.clusters:
            if not (self.create and cluster.get("acm-observability")):
                continue

            cluster_name = cluster["name"]
            storage_type = cluster.get("acm-observability-storage-type")
            base_error_str = f"cluster: {cluster_name} - storage type: {storage_type}"
            if storage_type not in OBSERVABILITY_SUPPORTED_STORAGE_TYPES:
                not_supported_clusters.append(base_error_str)
            else:
                missing_storage_data.extend(
                    self.check_missing_observability_storage_data(
                        cluster=cluster,
                        storage_type=storage_type,
                    )
                )

        if not_supported_clusters or missing_storage_data:
            if not_supported_clusters:
                _error_clusters = "\n".join(not_supported_clusters)
                self.logger.error(
                    "The following storage types are not supported for"
                    f" observability:\n{_error_clusters}\nsupported storage types are"
                    f" {OBSERVABILITY_SUPPORTED_STORAGE_TYPES}\n"
                )

            if missing_storage_data:
                _storage_clusters = "\n".join(missing_storage_data)
                self.logger.error(
                    "The following clusters are missing storage data for observability:\n{_storage_clusters}\n"
                )
            raise click.Abort()

    @staticmethod
    def check_missing_observability_storage_data(
        cluster,
        storage_type,
    ):
        missing_storage_data = []
        base_error_str = f"cluster: {cluster['name']} - storage type: {storage_type}"
        if storage_type == S3_STR:
            if not cluster.get("aws-access-key-id"):
                missing_storage_data.append(f"{base_error_str} is missing `acm-observability-s3-access-key-id`")
            if not cluster.get("aws-secret-access-key"):
                missing_storage_data.append(f"{base_error_str} is missing" " `acm-observability-s3-secret-access-key`")

        return missing_storage_data
