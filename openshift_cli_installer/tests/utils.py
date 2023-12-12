import json

from openshift_cli_installer.utils.cluster_versions import get_ipi_cluster_versions, set_clusters_versions


def update_aws_clusters_versions(clusters, _test=False):
    for _cluster_data in clusters:
        _cluster_data["stream"] = _cluster_data.get("stream", "stable")

    base_available_versions = get_all_versions(_test=_test)

    return set_clusters_versions(clusters=clusters, base_available_versions=base_available_versions)


def get_all_versions(_test=None):
    if _test:
        with open("openshift_cli_installer/tests/all_aws_versions.json") as fd:
            base_available_versions = json.load(fd)
    else:
        base_available_versions = get_ipi_cluster_versions()

    return base_available_versions
