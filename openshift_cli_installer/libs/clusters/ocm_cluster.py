from datetime import datetime, timedelta
import re
from typing import Any, Dict, List
import sys
from ocm_python_wrapper.cluster import Cluster
from ocm_python_wrapper.versions import Versions
from simple_logger.logger import get_logger

from openshift_cli_installer.libs.clusters.ocp_cluster import OCPCluster
from openshift_cli_installer.libs.user_input import UserInput
from openshift_cli_installer.utils.const import STAGE_STR
from pyhelper_utils.general import tts


version = sys.version_info
if version[0] == 3 and version[1] < 9:
    from functools import lru_cache as cache
else:
    from functools import cache  # type: ignore[no-redef]


class OcmCluster(OCPCluster):
    def __init__(self, ocp_cluster: Dict[str, Any], user_input: UserInput) -> None:
        super().__init__(ocp_cluster=ocp_cluster, user_input=user_input)
        self.logger = get_logger(f"{self.__class__.__module__}-{self.__class__.__name__}")

        if not self.user_input.destroy_from_s3_bucket_or_local_directory:
            self.osd_base_available_versions_dict: Dict[str, Dict[str, List[str]]] = {}
            self.rosa_base_available_versions_dict: Dict[str, Dict[str, List[str]]] = {}
            self.cluster["channel-group"] = self.cluster_info["channel-group"] = self.cluster.get(
                "channel-group", "stable"
            )
            self.cluster["multi-az"] = self.cluster_info["multi-az"] = self.cluster.get("multi-az", False)
            self.cluster["ocm-env"] = self.cluster_info["ocm-env"] = self.cluster.get("ocm-env", STAGE_STR)

            self._set_expiration_time()
            self.dump_cluster_data_to_file()

        self.prepare_cluster_data()
        self.cluster_object = Cluster(
            client=self.ocm_client,
            name=self.cluster_info["name"],
        )

    def _set_expiration_time(self) -> None:
        expiration_time = self.cluster.get("expiration-time")
        if expiration_time:
            _expiration_time = tts(ts=expiration_time)
            self.cluster["expiration-time"] = self.cluster_info["expiration-time"] = (
                f"{(datetime.now() + timedelta(seconds=_expiration_time)).isoformat()}Z"
            )

    @cache
    def get_osd_versions(self) -> None:
        updated_versions_dict: Dict[str, Dict[str, List[str]]] = {}
        for channel, versions in (
            Versions(client=self.ocm_client).get(channel_group=self.cluster_info["channel-group"]).items()
        ):
            updated_versions_dict[channel] = {}
            for version in versions:
                _version_key = re.findall(r"^\d+.\d+", version)[0]
                updated_versions_dict[channel].setdefault(_version_key, []).append(version)

        self.osd_base_available_versions_dict.update(updated_versions_dict)
