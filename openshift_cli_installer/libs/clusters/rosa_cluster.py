import os
import re
import shutil
from typing import Any, Dict

import click
import rosa.cli
from python_terraform import IsNotFlagged, Terraform
from simple_logger.logger import get_logger
import secrets
import string
from openshift_cli_installer.libs.clusters.ocm_cluster import OcmCluster
from openshift_cli_installer.libs.user_input import UserInput
from openshift_cli_installer.utils.cluster_versions import get_cluster_version_to_install
from openshift_cli_installer.utils.const import HYPERSHIFT_STR
from openshift_cli_installer.utils.general import (
    get_manifests_path,
    zip_and_upload_to_s3,
)
from ocp_resources.group import Group
from rosa.rosa_versions import get_rosa_versions
from timeout_sampler import TimeoutSampler
from clouds.aws.roles.roles import get_roles


class RosaCluster(OcmCluster):
    def __init__(self, ocp_cluster: Dict[str, Any], user_input: UserInput) -> None:
        super().__init__(ocp_cluster=ocp_cluster, user_input=user_input)
        self.logger = get_logger(f"{self.__class__.__module__}-{self.__class__.__name__}")
        if self.user_input.create:
            self.cluster_info["aws-account-id"] = self.user_input.aws_account_id
            self.assert_hypershift_missing_roles()
            self.rosa_base_available_versions_dict = get_rosa_versions(
                ocm_client=self.ocm_client,
                aws_region=self.cluster_info["region"],
                channel_group=self.cluster_info["channel-group"],
                hosted_cp=True if self.cluster_info["platform"] == HYPERSHIFT_STR else False,
            )
            self.cluster["version"] = get_cluster_version_to_install(
                wanted_version=self.cluster_info["user-requested-version"],
                base_versions_dict=self.rosa_base_available_versions_dict,
                platform=self.cluster_info["platform"],
                stream=self.cluster_info["stream"],
                log_prefix=self.log_prefix,
            )

        if not self.user_input.destroy_from_s3_bucket_or_local_directory:
            if self.cluster_info["platform"] == HYPERSHIFT_STR:
                self.terraform = Terraform()
                self.cluster["tags"] = "dns:external"
                self.cluster["machine-cidr"] = self.cluster.get("cidr", "10.0.0.0/16")

            self.dump_cluster_data_to_file()

    def terraform_init(self) -> None:
        self.logger.info(f"{self.log_prefix}: Init Terraform")
        # az_id example: us-east-2 -> ["use2-az1", "use2-az2"]
        az_id_prefix_match = re.match(r"(.*)-(\w).*-(\d)", self.cluster_info["region"])
        az_id_prefix = "".join(az_id_prefix_match.groups()) if az_id_prefix_match else ""
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

    def create_oidc(self) -> None:
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

    def delete_oidc(self) -> None:
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

    def create_operator_role(self) -> None:
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

    def delete_operator_role(self) -> None:
        self.logger.info(f"{self.log_prefix}: Delete operator role")
        name = self.cluster_info["name"]
        rosa.cli.execute(
            command=f"delete operator-roles --prefix={name} --cluster={name}",
            aws_region=self.cluster_info["region"],
            ocm_client=self.ocm_client,
        )

    def destroy_hypershift_vpc(self) -> None:
        self.terraform_init()
        self.logger.info(f"{self.log_prefix}: Destroy hypershift VPCs")
        rc, _, err = self.terraform.destroy(
            force=IsNotFlagged,
            auto_approve=True,
            capture_output=True,
        )
        if rc != 0:
            self.logger.error(f"{self.log_prefix}: Failed to destroy hypershift VPCs with error: {err}")

    def prepare_hypershift_vpc(self) -> None:
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

    def build_rosa_command(self) -> str:
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
            "name-prefix",
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

    def create_cluster(self) -> None:
        idp_user, idp_password = "", ""

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

            # Must be called right after the cluster is ready.
            self.add_cluster_info_to_cluster_object()

            if self.cluster_info["platform"] == HYPERSHIFT_STR:
                idp_user, idp_password = self.create_hypershift_idp()

            self.set_cluster_auth(idp_user=idp_user, idp_password=idp_password)
            self.logger.success(f"{self.log_prefix}: Cluster created successfully")

        except Exception as ex:
            self.logger.error(
                f"{self.log_prefix}: Failed to run cluster create\n{ex}",
            )
            self.set_cluster_auth()
            if self.user_input.must_gather_output_dir:
                self.collect_must_gather()

            self.destroy_cluster()
            raise click.Abort()

        if self.s3_bucket_name:
            zip_and_upload_to_s3(
                install_dir=self.cluster_info["cluster-dir"],
                s3_bucket_name=self.s3_bucket_name,
                s3_bucket_object_name=self.cluster_info["s3-object-name"],
            )

    def destroy_cluster(self) -> None:
        self.timeout_watch = self.start_time_watcher()
        should_raise = False
        exception = None

        try:
            res = rosa.cli.execute(
                command=f"delete cluster --cluster={self.cluster_info['name']}",
                ocm_client=self.ocm_client,
                aws_region=self.cluster_info["region"],
            )
            self.cluster_object.wait_for_cluster_deletion(wait_timeout=self.timeout_watch.remaining_time())
            self.remove_leftovers(out=res.get("out", ""))

        except Exception as ex:
            should_raise = True
            exception = ex

        if self.cluster_info["platform"] == HYPERSHIFT_STR:
            self.destroy_hypershift_vpc()
            self.delete_oidc()
            self.delete_operator_role()

        if should_raise:
            self.logger.error(f"{self.log_prefix}: Failed to run cluster destroy\n{exception}")
            raise click.Abort()

        self.logger.success(f"{self.log_prefix}: Cluster destroyed successfully")
        self.delete_cluster_s3_buckets()

    def remove_leftovers(self, out: str) -> None:
        leftovers = re.search(
            r"INFO: Once the cluster is uninstalled use the following commands to"
            r" remove"
            r" the above "
            r"aws resources(.*?)INFO:",
            out,
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

    def assert_hypershift_missing_roles(self) -> None:
        if self.cluster_info["platform"] == HYPERSHIFT_STR:
            hcp_roles = {
                "ManagedOpenShift-HCP-ROSA-Installer-Role",
                "ManagedOpenShift-HCP-ROSA-Support-Role",
                "ManagedOpenShift-HCP-ROSA-Worker-Role",
            }

            if missing_roles := hcp_roles - {role["RoleName"] for role in get_roles()}:
                self.logger.error(f"The following roles are missing for {HYPERSHIFT_STR} deployment: {missing_roles}")
                raise click.Abort()

    def create_hypershift_idp(self) -> tuple[str, str]:
        """
        For hypershift cluster create IDP to be able to log in to the cluster with user and password.

        Returns:
            tuple: idp_user and idp_password.
        """
        idp_user = "rosa-admin"
        idp_password = self.generate_hypershift_password()
        aws_region = self.cluster_info["region"]
        commands = [
            f"create idp -c {self.cluster_object.cluster_id} --type htpasswd --name rosa-htpasswd "
            f"--username={idp_user} --password={idp_password}",
            f"grant user cluster-admin --user={idp_user} --cluster={self.cluster_object.cluster_id}",
        ]
        rosa_command_success = True
        for command in commands:
            try:
                rosa.cli.execute(
                    command=command,
                    ocm_client=self.ocm_client,
                    aws_region=aws_region,
                )
            except Exception as ex:
                rosa_command_success = False
                self.logger.error(f"{self.log_prefix}: Failed to create IDP\n{ex}")
                break

        if rosa_command_success:
            try:
                for sampler in TimeoutSampler(
                    wait_timeout=300,
                    sleep=10,
                    func=Group,
                    client=self.ocp_client,
                    name="cluster-admins",
                ):
                    if sampler and idp_user in sampler.instance.users:
                        break

            except Exception as ex:
                self.logger.error(f"{self.log_prefix}: {idp_user} is not part of cluster-admins\n{ex}")

        return idp_user, idp_password

    @staticmethod
    def generate_hypershift_password() -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(20))
