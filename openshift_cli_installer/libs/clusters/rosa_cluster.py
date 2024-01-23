import os
import re
import shutil

import click
import rosa.cli
from python_terraform import IsNotFlagged, Terraform
from simple_logger.logger import get_logger

from openshift_cli_installer.libs.clusters.ocm_cluster import OcmCluster
from openshift_cli_installer.utils.cluster_versions import filter_versions
from openshift_cli_installer.utils.const import HYPERSHIFT_STR
from openshift_cli_installer.utils.general import (
    get_manifests_path,
    zip_and_upload_to_s3,
)
from clouds.aws.session_clients import iam_client


class RosaCluster(OcmCluster):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.logger = get_logger(f"{self.__class__.__module__}-{self.__class__.__name__}")

        if self.create:
            self.cluster_info["aws-account-id"] = self.aws_account_id
            self.assert_hypershift_missing_roles()
            self.get_rosa_versions()
            self.all_available_versions.update(
                filter_versions(
                    wanted_version=self.cluster_info["user-requested-version"],
                    base_versions_dict=self.rosa_base_available_versions_dict,
                    platform=self.cluster_info["platform"],
                    stream=self.cluster_info["stream"],
                )
            )
            self.set_cluster_install_version()

        if not kwargs.get("destroy_from_s3_bucket_or_local_directory"):
            if self.cluster_info["platform"] == HYPERSHIFT_STR:
                self.terraform = None
                self.cluster["tags"] = "dns:external"
                self.cluster["machine-cidr"] = self.cluster.get("cidr", "10.0.0.0/16")

            self.dump_cluster_data_to_file()

    def terraform_init(self):
        self.logger.info(f"{self.log_prefix}: Init Terraform")
        # az_id example: us-east-2 -> ["use2-az1", "use2-az2"]
        az_id_prefix = "".join(re.match(r"(.*)-(\w).*-(\d)", self.cluster_info["region"]).groups())
        cluster_parameters = {
            "aws_region": self.cluster_info["region"],
            "az_ids": [f"{az_id_prefix}-az1", f"{az_id_prefix}-az2"],
            "cluster_name": self.cluster_info["name"],
        }
        cidr = self.cluster.get("cidr")
        if cidr:
            cluster_parameters["cidr"] = cidr

        private_subnets = self.cluster.get("private-subnets")
        if private_subnets:
            cluster_parameters["private_subnets"] = private_subnets

        public_subnets = self.cluster.get("public-subnets")
        if public_subnets:
            cluster_parameters["public_subnets"] = public_subnets

        self.terraform = Terraform(working_dir=self.cluster_info["cluster-dir"], variables=cluster_parameters)
        shutil.copy(
            os.path.join(get_manifests_path(), "setup-vpc.tf"),
            self.cluster_info["cluster-dir"],
        )
        rc, out, err = self.terraform.init()
        if rc != 0:
            self.logger.error(f"{self.log_prefix}: Terraform init failed. Err: {err}, Out: {out}")
            raise click.Abort()

    def create_oidc(self):
        self.logger.info(f"{self.log_prefix}: Create OIDC config")
        res = rosa.cli.execute(
            command="create oidc-config --managed=true",
            aws_region=self.cluster_info["region"],
            ocm_client=self.ocm_client,
        )
        oidc_id = res["out"].get("id")
        if not oidc_id:
            self.logger.error(f"{self.log_prefix}: Failed to get OIDC config")
            raise click.Abort()

        self.cluster["oidc-config-id"] = self.cluster_info["oidc-config-id"] = oidc_id

    def delete_oidc(self):
        self.logger.info(f"{self.log_prefix}: Delete OIDC config")
        oidc_config_id = self.cluster_info.get("oidc-config-id")
        if not oidc_config_id:
            self.logger.warning(f"{self.log_prefix}: No OIDC config ID to delete")
            return

        rosa.cli.execute(
            command=f"delete oidc-config --oidc-config-id={oidc_config_id}",
            aws_region=self.cluster_info["region"],
            ocm_client=self.ocm_client,
        )

    def create_operator_role(self):
        self.logger.info(f"{self.log_prefix}: Create operator role")
        rosa.cli.execute(
            command=(
                "create operator-roles --hosted-cp"
                f" --prefix={self.cluster_info['name']} "
                f"--oidc-config-id={self.cluster_info['oidc-config-id']} "
                "--installer-role-arn="
                f"arn:aws:iam::{self.cluster_info['aws-account-id']}:role/ManagedOpenShift-HCP-ROSA-Installer-Role"
            ),
            aws_region=self.cluster_info["region"],
            ocm_client=self.ocm_client,
        )

    def delete_operator_role(self):
        self.logger.info(f"{self.log_prefix}: Delete operator role")
        name = self.cluster_info["name"]
        rosa.cli.execute(
            command=f"delete operator-roles --prefix={name} --cluster={name}",
            aws_region=self.cluster_info["region"],
            ocm_client=self.ocm_client,
        )

    def destroy_hypershift_vpc(self):
        self.terraform_init()
        self.logger.info(f"{self.log_prefix}: Destroy hypershift VPCs")
        rc, _, err = self.terraform.destroy(
            force=IsNotFlagged,
            auto_approve=True,
            capture_output=True,
        )
        if rc != 0:
            self.logger.error(f"{self.log_prefix}: Failed to destroy hypershift VPCs with error: {err}")
            raise click.Abort()

    def prepare_hypershift_vpc(self):
        self.terraform_init()
        self.logger.info(f"{self.log_prefix}: Preparing hypershift VPCs")
        self.terraform.plan(dir_or_plan="hypershift.plan")
        rc, _, err = self.terraform.apply(capture_output=True, skip_plan=True, auto_approve=True)
        if rc != 0:
            self.logger.error(
                f"{self.log_prefix}: Create hypershift VPC failed with error: {err}, rolling back.",
            )
            self.delete_oidc()
            self.delete_operator_role()
            # Clean up already created resources from the plan
            self.destroy_hypershift_vpc()
            raise click.Abort()

        terraform_output = self.terraform.output()
        private_subnet = terraform_output["cluster-private-subnet"]["value"]
        public_subnet = terraform_output["cluster-public-subnet"]["value"]
        self.cluster["subnet-ids"] = f'"{public_subnet},{private_subnet}"'

    def build_rosa_command(self):
        ignore_keys = (
            "name",
            "platform",
            "ocm-env",
            "timeout",
            "cidr",
            "private-subnets",
            "public-subnets",
            "acm",
            "acm-clusters",
            "aws-access-key-id",
            "aws-secret-access-key",
            "aws-account-id",
            "auto-region",
        )
        ignore_prefix = ("acm-observability", "gcp")
        name = self.cluster_info["name"]
        command = f"create cluster --sts --cluster-name={name} "
        if self.cluster_info["platform"] == HYPERSHIFT_STR:
            command += (
                f" --role-arn=arn:aws:iam::{self.cluster_info['aws-account-id']}:role/ManagedOpenShift-HCP-ROSA-Installer-Role "
                f"--support-role-arn=arn:aws:iam::{self.cluster_info['aws-account-id']}:role/ManagedOpenShift-HCP-ROSA-Support-Role "
                f" --worker-iam-role=arn:aws:iam::{self.cluster_info['aws-account-id']}:role/ManagedOpenShift-HCP-ROSA-Worker-Role "
                f"--hosted-cp --operator-roles-prefix={name} "
            )

        for _key, _val in self.cluster.items():
            if _key in ignore_keys or _key.startswith(ignore_prefix):
                continue

            command += f"--{_key}={_val} "

        return command

    def create_cluster(self):
        self.timeout_watch = self.start_time_watcher()
        if self.cluster_info["platform"] == HYPERSHIFT_STR:
            self.create_oidc()
            self.create_operator_role()
            self.prepare_hypershift_vpc()

        self.dump_cluster_data_to_file()

        try:
            rosa.cli.execute(
                command=self.build_rosa_command(),
                ocm_client=self.ocm_client,
                aws_region=self.cluster_info["region"],
            )

            self.cluster_object.wait_for_cluster_ready(wait_timeout=self.timeout_watch.remaining_time())
            self.set_cluster_auth()
            self.add_cluster_info_to_cluster_object()
            self.logger.success(f"{self.log_prefix}: Cluster created successfully")

        except Exception as ex:
            self.logger.error(
                f"{self.log_prefix}: Failed to run cluster create\n{ex}",
            )
            self.set_cluster_auth()
            if self.must_gather_output_dir:
                self.collect_must_gather()

            self.destroy_cluster()
            raise click.Abort()

        if self.s3_bucket_name:
            zip_and_upload_to_s3(
                uuid=self.cluster_info["shortuuid"],
                install_dir=self.cluster_info["cluster-dir"],
                s3_bucket_name=self.s3_bucket_name,
                s3_bucket_path=self.s3_bucket_path,
            )

    def destroy_cluster(self):
        self.timeout_watch = self.start_time_watcher()
        should_raise = False
        try:
            res = rosa.cli.execute(
                command=f"delete cluster --cluster={self.cluster_info['name']}",
                ocm_client=self.ocm_client,
                aws_region=self.cluster_info["region"],
            )
            self.cluster_object.wait_for_cluster_deletion(wait_timeout=self.timeout_watch.remaining_time())
            self.remove_leftovers(res=res)

        except Exception as ex:
            should_raise = ex

        if self.cluster_info["platform"] == HYPERSHIFT_STR:
            self.destroy_hypershift_vpc()
            self.delete_oidc()
            self.delete_operator_role()

        if should_raise:
            self.logger.error(f"{self.log_prefix}: Failed to run cluster destroy\n{should_raise}")
            raise click.Abort()

        self.logger.success(f"{self.log_prefix}: Cluster destroyed successfully")
        self.delete_cluster_s3_buckets()

    def remove_leftovers(self, res):
        leftovers = re.search(
            r"INFO: Once the cluster is uninstalled use the following commands to"
            r" remove"
            r" the above "
            r"aws resources(.*?)INFO:",
            res.get("out", ""),
            re.DOTALL,
        )
        if leftovers:
            for line in leftovers.group(1).splitlines():
                _line = line.strip()
                if _line.startswith("rosa"):
                    base_command = _line.split(maxsplit=1)[-1]
                    command = base_command.replace("-c ", "--cluster=")
                    command = command.replace("--prefix ", "--prefix=")
                    command = command.replace("--oidc-config-id ", "--oidc-config-id=")
                    rosa.cli.execute(
                        command=command,
                        ocm_client=self.ocm_client,
                        aws_region=self.cluster_info["region"],
                    )

    def assert_hypershift_missing_roles(self):
        if self.cluster_info["platform"] == HYPERSHIFT_STR:
            hcp_roles = {
                "ManagedOpenShift-HCP-ROSA-Installer-Role",
                "ManagedOpenShift-HCP-ROSA-Support-Role",
                "ManagedOpenShift-HCP-ROSA-Worker-Role",
            }

            if missing_roles := hcp_roles - {role["RoleName"] for role in iam_client().list_roles()["Roles"]}:
                self.logger.error(f"The following roles are missing for {HYPERSHIFT_STR} deployment: {missing_roles}")
                raise click.Abort()
