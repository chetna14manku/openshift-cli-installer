import os


def main():
    clusters = ""
    cmd = "poetry run python openshift_cli_installer/cli.py"
    os_env = os.environ
    cmd += f" --action={os_env['ACTION']}"
    cmd += f" --clusters-install-data-directory={os_env['CLUSTERS_INSTALL_DATA_DIRECTORY']}"
    if os_env.get("CLUSTERS_YAML_CONFIG_FILE"):
        cmd += f" --clusters-yaml-config-file={os_env['CLUSTERS_YAML_CONFIG_FILE']}"
    if os_env.get("PARALLEL") == "true":
        cmd += " --parallel"

    if os_env.get("CLUSTER1"):
        clusters += f" --cluster '{os_env['CLUSTER1']}'"
    if os_env.get("CLUSTER2"):
        clusters += f" --cluster '{os_env['CLUSTER2']}'"
    if os_env.get("CLUSTER3"):
        clusters += f" --cluster '{os_env['CLUSTER3']}'"

    cmd += f" {clusters}"

    if os_env.get("REGISTRY_CONFIG_FILE"):
        cmd += f" --registry-config-file={os_env['REGISTRY_CONFIG_FILE']}"
    if os_env.get("OCM_TOKEN"):
        cmd += f" --ocm-token={os_env['OCM_TOKEN']}"
    if os_env.get("SSH_KEY_FILE"):
        cmd += f" --ssh-key-file={os_env['SSH_KEY_FILE']}"
    if os_env.get("DOCKER_CONFIG_FILE"):
        cmd += f" --docker-config-file={os_env['DOCKER_CONFIG_FILE']}"
    if os_env.get("S3_BUCKET_NAME"):
        cmd += f" --s3-bucket-name={os_env['S3_BUCKET_NAME']}"
    if os_env.get("S3_BUCKET_PATH"):
        cmd += f" --s3-bucket-path={os_env['S3_BUCKET_PATH']}"
    if os_env.get("S3_BUCKET_PATH_UUID"):
        cmd += f" --s3-bucket-path-uuid={os_env['S3_BUCKET_PATH_UUID']}"
    if os_env.get("AWS_ACCESS_KEY_ID"):
        cmd += f" --aws-access-key-id={os_env['AWS_ACCESS_KEY_ID']}"
    if os_env.get("AWS_SECRET_ACCESS_KEY"):
        cmd += f" --aws-secret-access-key={os_env['AWS_SECRET_ACCESS_KEY']}"
    if os_env.get("AWS_ACCOUNT_ID"):
        cmd += f" --aws-account-id={os_env['AWS_ACCOUNT_ID']}"
    if os_env.get("GCP_SERVICE_ACCOUNT_FILE"):
        cmd += f" --gcp-service-account-file={os_env['GCP_SERVICE_ACCOUNT_FILE']}"
    if os_env.get("MUST_GATHER_OUTPUT_DIR"):
        cmd += f" --must-gather-output-dir={os_env['MUST_GATHER_OUTPUT_DIR']}"
    if os_env.get("DESTROY_CLUSTERS_FROM_S3_BUCKET") == "true":
        cmd += " --destroy-clusters-from-s3-bucket"
    if os_env.get("DESTROY_CLUSTERS_FROM_S3_BUCKET_QUERY"):
        cmd += f" --destroy-clusters-from-s3-bucket-query={os_env['DESTROY_CLUSTERS_FROM_S3_BUCKET_QUERY']}"
    if os_env.get("DESTROY_CLUSTERS_FROM_INSTALL_DATA_DIRECTORY") == "true":
        cmd += " --destroy-clusters-from-install-data-directory"
    if os_env.get("DESTROY_CLUSTERS_FROM_INSTALL_DATA_DIRECTORY_USING_S3_BUCKET") == "true":
        cmd += " --destroy-clusters-from-install-data-directory-using-s3-bucket"

    print(cmd)


if __name__ == "__main__":
    main()
