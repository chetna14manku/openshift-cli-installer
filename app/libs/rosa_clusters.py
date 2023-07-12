import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import rosa.cli
from ocm_python_wrapper.cluster import Cluster
from ocm_python_wrapper.ocm_client import OCMPythonClient
from ocp_resources.job import Job
from ocp_resources.resource import get_client
from ocp_resources.utils import TimeoutSampler
from python_terraform import IsNotFlagged, Terraform, TerraformCommandError
from utils.const import HYPERSHIFT_STR


def time_string_to_seconds(time_string):
    time_and_unit = re.match(r"(?P<time>\d+)(?P<unit>\w)", time_string).groupdict()
    _time = int(time_and_unit["time"])
    _unit = time_and_unit["unit"]
    if _unit == "s":
        return _time
    elif _unit == "m":
        return _time * 60
    elif _unit == "h":
        return _time * 60 * 60
    return int(time_string)


def get_ocm_client(ocm_token, ocm_env):
    return OCMPythonClient(
        token=ocm_token,
        endpoint="https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token",
        api_host=ocm_env,
        discard_unknown_keys=True,
    ).client


def create_oidc(cluster_data):
    aws_region = cluster_data["region"]
    oidc_prefix = cluster_data["cluster-name"]
    ocm_token, ocm_env, _ = extract_ocm_data_from_cluster_data(cluster_data)
    ocm_client = get_ocm_client(ocm_token, ocm_env)
    for cmd in (
        f"create oidc-config --managed=false --prefix={oidc_prefix}",
        f"list oidc-config --region={aws_region}",
    ):
        res = rosa.cli.execute(
            command=cmd,
            aws_region=aws_region,
            ocm_client=ocm_client,
        )["out"]

    _oidc_config_id = [
        oidc_config["id"]
        for oidc_config in res
        if oidc_prefix in oidc_config["secret_arn"]
    ][0]
    cluster_data["oidc-config-id"] = _oidc_config_id
    return cluster_data


def terraform_init(cluster_data):
    aws_region = cluster_data["region"]
    # az_id example: us-east-2 -> ["use2-az1", "use2-az2"]
    az_id_prefix = "".join(re.match(r"(.*)-(\w).*-(\d)", aws_region).groups())
    cluster_parameters = {
        "aws_region": aws_region,
        "az_ids": [f"{az_id_prefix}-az1", f"{az_id_prefix}-az2"],
        "cluster_name": cluster_data["cluster-name"],
    }

    terraform = Terraform(
        working_dir=cluster_data["install-dir"], variables=cluster_parameters
    )
    terraform.init()
    return terraform


def destroy_hypershift_vpc(cluster_data):
    terraform = terraform_init(cluster_data)
    terraform.destroy(
        force=IsNotFlagged,
        auto_approve=True,
        capture_output=False,
        raise_on_error=True,
    )


def prepare_hypershift_vpc(cluster_data):
    shutil.copy("app/manifests/setup-vpc.tf", cluster_data)
    terraform = terraform_init(cluster_data=cluster_data)
    try:
        terraform.plan(dir_or_plan="rosa.plan")
        terraform.apply(capture_output=False, skip_plan=True, raise_on_error=True)
    except TerraformCommandError:
        # Clean up already created resources from the plan
        terraform.destroy(
            force=IsNotFlagged,
            auto_approve=True,
            capture_output=False,
            raise_on_error=True,
        )
        raise


def extract_ocm_data_from_cluster_data(cluster_data):
    ocm_token = cluster_data["ocm-token"]
    ocm_env = cluster_data["ocm-env"]
    ocm_env_url = (
        None if ocm_env == "production" else f"https://api.{ocm_env}.openshift.com"
    )
    return ocm_token, ocm_env, ocm_env_url


def get_cluster_object(ocm_token, ocm_env, cluster_data):
    ocm_client = get_ocm_client(ocm_token, ocm_env)
    for sample in TimeoutSampler(
        wait_timeout=time_string_to_seconds(time_string="5m"),
        sleep=1,
        func=Cluster,
        client=ocm_client,
        name=cluster_data["cluster-name"],
    ):
        if sample and sample.exists:
            return sample


def prepare_managed_clusters_data(clusters, ocm_token, ocm_env):
    for _cluster in clusters:
        _cluster["cluster-name"] = _cluster["name"]
        _cluster["ocm-token"] = ocm_token
        _cluster["ocm-env"] = ocm_env
        _cluster["timeout"] = time_string_to_seconds(
            time_string=_cluster.get("timeout", "30m")
        )
        if _cluster["platform"] == HYPERSHIFT_STR:
            _cluster["hosted-cp"] = "true"
            _cluster["tags"] = "dns:external"
            _cluster["machine-cidr"] = "10.0.0.0/16"

        expiration_time = _cluster.get("expiration-time")
        if expiration_time:
            _cluster[
                "expiration-time"
            ] = f"{(datetime.now() + timedelta(hours=expiration_time)).isoformat()}Z"

    return clusters


def rosa_create_cluster(cluster_data):
    hosted_cp_arg = "--hosted-cp"
    ignore_keys = ("name", "platform", "ocm-env", "ocm-token", "install-dir", "timeout")
    ocm_token, ocm_env, ocm_env_url = extract_ocm_data_from_cluster_data(cluster_data)
    command = "create cluster --sts "
    command_kwargs = {
        f"--{_key}={_val}"
        for _key, _val in cluster_data.items()
        if _key not in ignore_keys
    }

    if cluster_data["platform"] == HYPERSHIFT_STR:
        cluster_data = create_oidc(cluster_data=cluster_data)
        prepare_hypershift_vpc(cluster_data=cluster_data)

    for cmd in command_kwargs:
        if hosted_cp_arg in cmd:
            command += f"{hosted_cp_arg} "
        else:
            command += f"{cmd} "

    rosa.cli.execute(
        command=command,
        ocm_env=ocm_env_url,
        token=ocm_token,
        aws_region=cluster_data["region"],
    )

    cluster_object = get_cluster_object(
        ocm_token=ocm_token, ocm_env=ocm_env, cluster_data=cluster_data
    )
    cluster_object.wait_for_cluster_ready(wait_timeout=cluster_data["timeout"])

    kubeconfig_path = os.path.join(cluster_data["install-dir"], "auth")
    Path(kubeconfig_path).mkdir(parents=True, exist_ok=True)

    with open(os.path.join(kubeconfig_path, "kubeconfig"), "w") as fd:
        fd.write(cluster_object.credentials.kubeconfig)

    ocp_client = get_client(config_dict=cluster_object.kubeconfig)
    job = Job(
        client=ocp_client, name="osd-cluster-ready", namespace="openshift-monitoring"
    )
    job.wait_for_condition(
        condition=job.Condition.COMPLETE,
        status="True",
        timeout=time_string_to_seconds(time_string="40m"),
    )


def rosa_delete_cluster(cluster_data):
    ocm_token, ocm_env, ocm_env_url = extract_ocm_data_from_cluster_data(cluster_data)
    command = f"delete cluster --cluster={cluster_data['cluster-name']}"
    res = rosa.cli.execute(
        command=command,
        ocm_env=ocm_env_url,
        token=ocm_token,
        aws_region=cluster_data["region"],
    )
    cluster_object = get_cluster_object(
        ocm_token=ocm_token, ocm_env=ocm_env, cluster_data=cluster_data
    )
    cluster_object.wait_for_cluster_deletion(wait_timeout=cluster_data["timeout"])
    leftovers = re.search(
        r"INFO: Once the cluster is uninstalled use the following commands to remove the above aws resources(.*?)INFO:",
        res.get("out", ""),
        re.DOTALL,
    )
    if leftovers:
        for line in leftovers.group(1).splitlines():
            _line = line.strip()
            if _line.startswith("rosa"):
                base_command = _line.split(maxsplit=1)[-1]
                str_to_replace = re.search(r"-c.*", base_command).group(0)
                command = str_to_replace.replace("-c ", "--cluster=")
                rosa.cli.execute(
                    command=command,
                    ocm_env=ocm_env_url,
                    token=ocm_token,
                    aws_region=cluster_data["region"],
                )

    if cluster_data["platform"] == HYPERSHIFT_STR:
        destroy_hypershift_vpc(cluster_data=cluster_data)
