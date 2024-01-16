import click
from simple_logger.logger import get_logger

from openshift_cli_installer.libs.clusters.ocm_cluster import OcmCluster
from openshift_cli_installer.utils.cluster_versions import filter_versions
from openshift_cli_installer.utils.const import AWS_OSD_STR, GCP_OSD_STR
from openshift_cli_installer.utils.general import zip_and_upload_to_s3, get_dict_from_json


class OsdCluster(OcmCluster):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.logger = get_logger(f"{self.__class__.__module__}-{self.__class__.__name__}")

        self.gcp_service_account = get_dict_from_json(gcp_service_account_file=self.gcp_service_account_file)

        if self.create:
            self.cluster_info["aws-account-id"] = self.aws_account_id
            self.get_osd_versions()
            self.all_available_versions.update(
                filter_versions(
                    wanted_version=self.cluster_info["user-requested-version"],
                    base_versions_dict=self.osd_base_available_versions_dict,
                    platform=self.cluster_info["platform"],
                    stream=self.cluster_info["stream"],
                )
            )

            self.set_cluster_install_version()

        if kwargs.get("destroy_from_s3_bucket_or_local_directory"):
            self.dump_cluster_data_to_file()

    def create_cluster(self):
        self.timeout_watch = self.start_time_watcher()
        try:
            ocp_version = (
                self.cluster["version"]
                if self.cluster_info["channel-group"] == "stable"
                else f"{self.cluster_info['version']}-{self.cluster_info['channel-group']}"
            )
            provision_osd_kwargs = {
                "wait_for_ready": True,
                "wait_timeout": self.timeout_watch.remaining_time(),
                "region": self.cluster_info["region"],
                "ocp_version": ocp_version,
                "replicas": self.cluster_info["replicas"],
                "compute_machine_type": self.cluster_info["compute-machine-type"],
                "multi_az": self.cluster_info["multi-az"],
                "channel_group": self.cluster_info["channel-group"],
                "platform": self.cluster_info["platform"].replace("-osd", ""),
            }

            expiration_time = self.cluster_info.get("expiration-time")
            if expiration_time:
                provision_osd_kwargs["expiration_time"] = expiration_time

            if self.cluster_info["platform"] == AWS_OSD_STR:
                provision_osd_kwargs.update({
                    "aws_access_key_id": self.cluster_info["aws-access-key-id"],
                    "aws_account_id": self.cluster_info["aws-account-id"],
                    "aws_secret_access_key": self.cluster_info["aws-secret-access-key"],
                })
            elif self.cluster_info["platform"] == GCP_OSD_STR:
                provision_osd_kwargs.update({"gcp_service_account": self.gcp_service_account})

            self.cluster_object.provision_osd(**provision_osd_kwargs)
            self.add_cluster_info_to_cluster_object()
            self.set_cluster_auth()

            self.logger.success(f"{self.log_prefix}: Cluster created successfully")

        except Exception as ex:
            self.logger.error(
                f"{self.log_prefix}: Failed to run cluster create \n{ex}",
            )
            self.set_cluster_auth()

            if self.must_gather_output_dir:
                self.collect_must_gather()

            self.destroy_cluster()
            raise click.Abort()

        if self.s3_bucket_name:
            zip_and_upload_to_s3(
                install_dir=self.cluster_info["cluster-dir"],
                s3_bucket_name=self.s3_bucket_name,
                s3_bucket_path=self.s3_bucket_path,
                uuid=self.cluster_info["shortuuid"],
            )

    def destroy_cluster(self):
        self.timeout_watch = self.start_time_watcher()
        try:
            self.cluster_object.delete(timeout=self.timeout_watch.remaining_time())
            self.logger.success(f"{self.log_prefix}: Cluster destroyed successfully")
            self.delete_cluster_s3_buckets()
        except Exception as ex:
            self.logger.error(f"{self.log_prefix}: Failed to run cluster delete\n{ex}")
            raise click.Abort()
