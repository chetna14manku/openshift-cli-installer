import base64
import copy
import os
import shlex
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import click
import shortuuid
import yaml
from clouds.aws.session_clients import s3_client
from ocm_python_wrapper.ocm_client import OCMPythonClient
from ocp_resources.cluster_version import ClusterVersion
from ocp_resources.managed_cluster import ManagedCluster
from ocp_resources.multi_cluster_hub import MultiClusterHub
from ocp_resources.multi_cluster_observability import MultiClusterObservability
from ocp_resources.namespace import Namespace
from ocp_resources.route import Route
from ocp_resources.secret import Secret
from timeout_sampler import TimeoutWatch
from ocp_utilities.infra import get_client
from ocp_utilities.must_gather import run_must_gather
from ocp_utilities.utils import run_command
from simple_logger.logger import get_logger

from openshift_cli_installer.libs.user_input import UserInput
from openshift_cli_installer.utils.cluster_versions import (
    get_cluster_stream,
    get_split_version,
)
from openshift_cli_installer.utils.const import (
    CLUSTER_DATA_YAML_FILENAME,
    PRODUCTION_STR,
    S3_STR,
    STAGE_STR,
    TIMEOUT_60MIN,
)
from openshift_cli_installer.utils.general import tts


class OCPCluster(UserInput):
    def __init__(self, ocp_cluster, **kwargs):
        super().__init__(**kwargs)
        self.logger = get_logger(f"{self.__class__.__module__}-{self.__class__.__name__}")

        self.cluster = ocp_cluster
        destroy_from_s3_bucket_or_local_directory = kwargs.get("destroy_from_s3_bucket_or_local_directory")
        if destroy_from_s3_bucket_or_local_directory:
            self.cluster_info = self.cluster["cluster_info"]
            self.s3_bucket_name = self.s3_bucket_name or self.cluster["cluster_info"].get("s3_bucket_name")
            self.s3_bucket_path = self.s3_bucket_path or self.cluster["cluster_info"].get("s3_bucket_path")
        else:
            self.cluster_info = copy.deepcopy(self.cluster)

            self.cluster_info.update({
                "display-name": self.cluster_info["name"],
                "user-requested-version": self.cluster_info["version"],
                "shortuuid": self.s3_bucket_path_uuid or shortuuid.uuid(),
                "aws-access-key-id": self.cluster.pop("aws-access-key-id", None),
                "aws-secret-access-key": self.cluster.pop("aws-secret-access-key", None),
                "acm": self.cluster.get("acm") is True,
                "acm-observability": self.cluster.get("acm-observability") is True,
                "acm-observability-s3-region": self.cluster.get(
                    "acm-observability-s3-region", self.cluster_info["region"]
                ),
            })
            self.all_available_versions = {}

            self.cluster_info["stream"] = get_cluster_stream(cluster_data=self.cluster)

            self.cluster_info["cluster-dir"] = cluster_dir = self.cluster.pop(
                "cluster_dir",
                os.path.join(
                    self.clusters_install_data_directory,
                    self.cluster_info["platform"],
                    self.cluster_info["name"],
                ),
            )
            self.cluster_info["auth-path"] = auth_path = os.path.join(cluster_dir, "auth")
            self.cluster_info["kubeconfig-path"] = os.path.join(auth_path, "kubeconfig")
            Path(auth_path).mkdir(parents=True, exist_ok=True)

            self._add_s3_bucket_data()

        self.log_prefix = (
            f"[C:{self.cluster_info['name']}|P:{self.cluster_info['platform']}|R:{self.cluster_info['region']}]"
        )
        self.timeout = tts(ts=self.cluster.get("timeout", TIMEOUT_60MIN))

        if not destroy_from_s3_bucket_or_local_directory:
            self.dump_cluster_data_to_file()

        self.ocm_client = None
        self.ssh_key = None
        self.pull_secret = None
        self.timeout_watch = None
        self.cluster_object = None
        self.ocp_client = None

    @property
    def to_dict(self):
        return self.__dict__

    def start_time_watcher(self):
        if self.timeout_watch:
            self.logger.info(
                f"{self.log_prefix}: Reusing timeout watcher, time left: "
                f"{timedelta(seconds=self.timeout_watch.remaining_time())}"
            )
            return self.timeout_watch

        self.logger.info(f"{self.log_prefix}: Start timeout watcher, time left: {timedelta(seconds=self.timeout)}")
        return TimeoutWatch(timeout=self.timeout)

    def prepare_cluster_data(self):
        supported_envs = (PRODUCTION_STR, STAGE_STR)
        if self.cluster_info["ocm-env"] not in supported_envs:
            self.logger.error(
                f"{self.log_prefix}: got unsupported OCM env -"
                f" {self.cluster_info['ocm-env']}, supported envs: {supported_envs}"
            )
            raise click.Abort()

        self.ocm_client = self.get_ocm_client()

    def get_ocm_client(self):
        return OCMPythonClient(
            token=self.ocm_token,
            endpoint="https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token",
            api_host=self.cluster_info["ocm-env"],
            discard_unknown_keys=True,
        ).client

    def _add_s3_bucket_data(self):
        self.cluster_info["s3-object-name"] = (
            f"{f'{self.s3_bucket_path}/' if self.s3_bucket_path else ''}"
            f"{self.cluster_info['name']}-{self.cluster_info['shortuuid']}.zip"
        )

    def set_cluster_install_version(self):
        version = self.cluster_info["user-requested-version"]
        version_key = get_split_version(version=version)
        all_stream_versions = self.all_available_versions[self.cluster_info["stream"]][version_key]
        err_msg = f"{self.log_prefix}: Cluster version {version} not found for stream {self.cluster_info['stream']}"
        if len(version.split(".")) == 3:
            for _ver in all_stream_versions["versions"]:
                if version in _ver:
                    self.cluster["version"] = self.cluster_info["version"] = _ver
                    break
            else:
                self.logger.error(f"{err_msg}")
                raise click.Abort()

        elif len(version.split(".")) < 2:
            self.logger.error(
                f"{self.log_prefix}: Version must be at least x.y (4.3), got {version}",
            )
            raise click.Abort()
        else:
            try:
                self.cluster["version"] = self.cluster_info["version"] = all_stream_versions["latest"]
            except KeyError:
                self.logger.error(f"{err_msg}")
                raise click.Abort()

        self.logger.success(f"{self.log_prefix}: Cluster version set to {self.cluster_info['version']}")

    def dump_cluster_data_to_file(self):
        if not self.create:
            return

        _cluster_data = {}
        keys_to_pop = (
            "name",
            "ocm_client",
            "ocp_client",
            "cluster_object",
            "logger",
            "clusters",
            "user_kwargs",
            "log_prefix",
            "create",
            "action",
            "clusters_yaml_config_file",
            "all_available_versions",
            "gcp_service_account",
            "osd_base_available_versions_dict",
            "rosa_base_available_versions_dict",
            "timeout",
            "terraform",
            "timeout_watch",
            "ipi_base_available_versions",
            "_already_processed",
        )
        for _key, _val in self.to_dict.items():
            if _key in keys_to_pop or not _val:
                continue

            _cluster_data[_key] = _val

        _cluster_data_yaml_file = os.path.join(self.cluster_info["cluster-dir"], CLUSTER_DATA_YAML_FILENAME)
        self.logger.info(f"{self.log_prefix}: Writing cluster data to {_cluster_data_yaml_file}")
        with open(_cluster_data_yaml_file, "w") as fd:
            fd.write(yaml.dump(_cluster_data))

    def collect_must_gather(self):
        name = self.cluster_info["name"]
        try:
            target_dir = os.path.join(
                self.must_gather_output_dir,
                "must-gather",
                self.cluster_info["platform"],
                name,
            )
        except Exception as ex:
            self.logger.error(f"{self.log_prefix}: Failed to get data; must-gather could not be executed on: {ex}")
            return

        try:
            kubeconfig_path = self.cluster_info["kubeconfig-path"]
            if not os.path.exists(kubeconfig_path):
                self.logger.error(f"{self.log_prefix}: kubeconfig does not exist; cannot run" " must-gather.")
                return

            self.logger.info(f"{self.log_prefix}: Prepare must-gather target extracted directory {target_dir}.")
            Path(target_dir).mkdir(parents=True, exist_ok=True)

            click.echo(f"Collect must-gather for cluster {name} running on {self.cluster_info['platform']}")
            run_must_gather(
                target_base_dir=target_dir,
                kubeconfig=kubeconfig_path,
            )
            self.logger.success(f"{self.log_prefix}: must-gather collected")

        except Exception as ex:
            self.logger.error(
                f"{self.log_prefix}: Failed to run must-gather \n{ex}",
            )

            self.logger.info(f"{self.log_prefix}: Delete must-gather target directory {target_dir}.")
            shutil.rmtree(target_dir)

    def add_cluster_info_to_cluster_object(self):
        """
        Adds cluster information to the given clusters data dictionary.

        `cluster-id`, `api-url` and `console-url` (when available) will be added to `cluster_data`.
        """
        if self.cluster_object:
            self.ocp_client = self.cluster_object.ocp_client
            self.cluster_info["cluster-id"] = self.cluster_object.cluster_id

        else:
            self.ocp_client = get_client(config_file=self.cluster_info["kubeconfig-path"])
            # Unmanaged clusters name is set to cluster id
            self.cluster_info["cluster-id"] = self.cluster_info["display-name"] = ClusterVersion(
                client=self.ocp_client, name="version"
            ).instance.spec.clusterID

        self.cluster_info["api-url"] = self.ocp_client.configuration.host
        console_route = Route(name="console", namespace="openshift-console", client=self.ocp_client)
        if console_route.exists:
            route_spec = console_route.instance.spec
            self.cluster_info["console-url"] = f"{route_spec.port.targetPort}://{route_spec.host}"

        self.dump_cluster_data_to_file()

    def set_cluster_auth(self):
        auth_path = self.cluster_info["auth-path"]
        Path(auth_path).mkdir(parents=True, exist_ok=True)

        with open(os.path.join(auth_path, "kubeconfig"), "w") as fd:
            fd.write(yaml.dump(self.cluster_object.kubeconfig))

        with open(os.path.join(auth_path, "kubeadmin-password"), "w") as fd:
            fd.write(self.cluster_object.kubeadmin_password)

        self.dump_cluster_data_to_file()

    def delete_cluster_s3_buckets(self):
        if s3_file := self.cluster_info.get("s3-object-name"):
            self.logger.info(f"{self.log_prefix}: Deleting S3 file {s3_file} from {self.s3_bucket_name}")
            s3_client().delete_object(Bucket=self.s3_bucket_name, Key=s3_file)
            self.logger.success(f"{self.log_prefix}: {s3_file} deleted ")

    def install_acm(self):
        self.logger.info(f"{self.log_prefix}: Installing ACM")
        run_command(
            command=shlex.split(f"cm install acm --kubeconfig {self.cluster_info['kubeconfig-path']}"),
        )
        cluster_hub = MultiClusterHub(
            client=self.ocp_client,
            name="multiclusterhub",
            namespace="open-cluster-management",
        )
        cluster_hub.wait_for_status(
            status=cluster_hub.Status.RUNNING,
            timeout=self.timeout_watch.remaining_time(),
        )

        self.logger.success(f"{self.log_prefix}: ACM installed successfully")

    def enable_observability(self):
        thanos_secret_data = None
        _s3_client = None
        bucket_name = f"{self.cluster_info['name']}-observability-{self.cluster_info['shortuuid']}"

        if self.cluster_info["acm-observability-storage-type"] == S3_STR:
            region = self.cluster_info["acm-observability-s3-region"]
            _s3_client = s3_client(region_name=region)
            s3_secret_data = f"""
            type: {S3_STR}
            config:
              bucket: {bucket_name}
              endpoint: s3.{region}.amazonaws.com
              insecure: true
              access_key: {self.cluster_info["aws-access-key-id"]}
              secret_key: {self.cluster_info["aws-secret-access-key"]}
            """
            s3_secret_data_bytes = s3_secret_data.encode("ascii")
            thanos_secret_data = {"thanos.yaml": base64.b64encode(s3_secret_data_bytes).decode("utf-8")}
            self.logger.info(f"{self.log_prefix}: Create S3 bucket {bucket_name} in {region}")
            _s3_client.create_bucket(
                Bucket=bucket_name.lower(),
                CreateBucketConfiguration={"LocationConstraint": region},
            )

        try:
            open_cluster_management_observability_ns = Namespace(
                client=self.ocp_client, name="open-cluster-management-observability"
            )
            open_cluster_management_observability_ns.deploy(wait=True)
            openshift_pull_secret = Secret(client=self.ocp_client, name="pull-secret", namespace="openshift-config")
            observability_pull_secret = Secret(
                client=self.ocp_client,
                name="multiclusterhub-operator-pull-secret",
                namespace=open_cluster_management_observability_ns.name,
                data_dict={".dockerconfigjson": openshift_pull_secret.instance.data[".dockerconfigjson"]},
                type="kubernetes.io/dockerconfigjson",
            )
            observability_pull_secret.deploy(wait=True)
            thanos_secret = Secret(
                client=self.ocp_client,
                name="thanos-object-storage",
                namespace=open_cluster_management_observability_ns.name,
                type="Opaque",
                data_dict=thanos_secret_data,
            )
            thanos_secret.deploy(wait=True)

            multi_cluster_observability_data = {
                "name": thanos_secret.name,
                "key": "thanos.yaml",
            }
            multi_cluster_observability = MultiClusterObservability(
                client=self.ocp_client,
                name="observability",
                metric_object_storage=multi_cluster_observability_data,
            )
            multi_cluster_observability.deploy(wait=True)
            multi_cluster_observability.wait_for_condition(
                condition=multi_cluster_observability.Condition.READY,
                status=multi_cluster_observability.Condition.Status.TRUE,
                timeout=self.timeout_watch.remaining_time(),
            )
            self.logger.success(f"{self.log_prefix}: Observability enabled")
        except Exception as ex:
            self.logger.error(f"{self.log_prefix}: Failed to enable observability. error: {ex}")

            if self.cluster_info["acm-observability-storage-type"] == S3_STR:
                for _bucket in _s3_client.list_buckets()["Buckets"]:
                    if _bucket["Name"] == bucket_name:
                        _s3_client.delete_bucket(Bucket=bucket_name)

            raise click.Abort()

    def attach_clusters_to_acm_hub(self, clusters):
        futures = []
        with ThreadPoolExecutor() as executor:
            for _managed_acm_cluster in self.cluster_info.get("acm-clusters"):
                _managed_acm_cluster_object = clusters.get_cluster_object_by_name(name=_managed_acm_cluster)
                _managed_cluster_name = _managed_acm_cluster_object.cluster_info["name"]
                managed_acm_cluster_kubeconfig = self.get_cluster_kubeconfig_from_install_dir(
                    cluster_name=_managed_cluster_name,
                    cluster_platform=_managed_acm_cluster_object.cluster_info["platform"],
                )
                action_kwargs = {
                    "managed_acm_cluster_name": _managed_cluster_name,
                    "acm_cluster_kubeconfig": self.cluster_info["kubeconfig-path"],
                    "managed_acm_cluster_kubeconfig": managed_acm_cluster_kubeconfig,
                }

                self.logger.info(f"{self.log_prefix}: Attach {_managed_cluster_name} to ACM hub")

                if self.parallel:
                    futures.append(executor.submit(self.attach_cluster_to_acm, **action_kwargs))
                else:
                    self.attach_cluster_to_acm(**action_kwargs)

            if futures:
                for result in as_completed(futures):
                    _exception = result.exception()
                    if _exception:
                        self.logger.error(
                            f"{self.log_prefix}: Failed to attach"
                            f" {_managed_cluster_name} to ACM hub Error: {_exception}"
                        )
                        raise click.Abort()

    def attach_cluster_to_acm(
        self,
        managed_acm_cluster_name,
        acm_cluster_kubeconfig,
        managed_acm_cluster_kubeconfig,
    ):
        if not self.ocp_client:
            self.ocp_client = get_client(config_file=acm_cluster_kubeconfig)

        self.logger.info(f"{self.log_prefix}: Attach {managed_acm_cluster_name} to ACM hub")

        run_command(
            command=shlex.split(
                f"cm --kubeconfig {acm_cluster_kubeconfig} attach cluster --cluster"
                f" {managed_acm_cluster_name} --cluster-kubeconfig"
                f" {managed_acm_cluster_kubeconfig}  --wait"
            ),
            check=False,
            verify_stderr=False,
        )

        managed_cluster = ManagedCluster(client=self.ocp_client, name=managed_acm_cluster_name)
        managed_cluster.wait_for_condition(
            condition="ManagedClusterImportSucceeded",
            status=managed_cluster.Condition.Status.TRUE,
            timeout=self.timeout_watch.remaining_time(),
        )
        self.logger.success(
            f"{self.log_prefix}: attached {managed_acm_cluster_name} to cluster {self.cluster_info['name']}"
        )

    def get_cluster_kubeconfig_from_install_dir(self, cluster_name, cluster_platform):
        cluster_install_dir = os.path.join(self.clusters_install_data_directory, cluster_platform, cluster_name)
        if not os.path.exists(cluster_install_dir):
            self.logger.error(f"{self.log_prefix}: ACM managed cluster data dir not found in {cluster_install_dir}")
            raise click.Abort()

        return os.path.join(cluster_install_dir, "auth", "kubeconfig")
