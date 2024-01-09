# openshift-cli-installer
Basic Openshift install CLI wrapper.  
The tool allows deploying or deletion of one or more clusters.  
The clusters can be deployed on different platforms; currently supported platforms: AWS IPI, AWS OSD, GCP OSD, ROSA and Hypershift.  
Each cluster can be provided with different configuration options, such as worker type, number of workers etc.

## Tools
* AWS IPI installation uses openshift-installer cli which is extracted from the cluster's target version.  
The binary is taken from `quay.io/openshift-release-dev/ocp-release:<target version>`
* ROSA and Hypershift installation uses the latest ROSA CLI

### Container
Image locate at [openshift-cli-installer](https://quay.io/repository/redhat_msi/openshift-cli-installer)  
To pull the image: `podman pull quay.io/openshift-cli-installer`

### Create clusters from YAML file
User can create/destroy clusters by sending YAML file instead with CLI args
Example YAML file can be found [here](openshift_cli_installer/manifests/clusters.example.yaml)
pass `--clusters-yaml-config-file=.local/clusters-example.yaml` to use YAML file.
Action also can be passed to the CLI as `--action create/destroy` instead of specifying the action in the YAML file.
`--action create --clusters-yaml-config-file=.local/clusters-example.yaml`

### Global CLI configuration
* `--clusters-install-data-directory`: Clusters configurations are written to `<clusters-install-data-directory><platform><cluster name>`; write permissions are needed.
    * `<cluster directory>/auth` contains `kubeconfig` and `kubeadmin-password` files
* `--parallel`: To create / destroy clusters in parallel
* Pass `--s3-bucket-name` (and optionally `--s3-bucket-path`) to backup <cluster directory> in an S3 bucket.  
* `--ocm-token`: OCM token, defaults to `OCM_TOKEN` environment variable.
* `--must-gather-output-dir`: Path to must-gather output dir. `must-gather` will try to collect data when cluster installation fails and cluster can be accessed.

* AWS IPI clusters:
  * The installer output is saved in the `<cluster directory>`.
  * The data is used for cluster destroy.
  * `platform=aws`: Must pass in cluster parameters
  * `base-domain`: cluster parameter is mandatory
  * `--registry-config-file`: registry-config json file path, can be obtained from [openshift local cluster](https://console.redhat.com/openshift/create/local)
  * `--docker-config-file`: Path to Docker config.json file, defaults to `~/.docker/config.json`. File must include token for `registry.ci.openshift.org`
  * `--ssh-key-file`: id_rsa file path

* GCP IPI clusters:
  * The installer output is saved in the `<cluster directory>`.
  * The data is used for cluster destroy.
  * `platform=gcp`: Must pass in cluster parameters
  * `base-domain`: cluster parameter is mandatory
  * `--gcp-service-account-file`: Path to GCP service account json. The file will be copied to specific path `~/.gcp/osServiceAccount.json` for installer .
     Follow [these](#steps-to-create-gcp-service-account-file) steps to get the ServiceAccount file.

* ROSA / Hypershift clusters:
  * `platform=rosa`: Must pass in cluster parameters
  * `--aws-account-id`: AWS account ID for Hypershift clusters

* AWS OSD clusters:
  * `platform=aws-osd`: Must pass in cluster parameters
  * `--aws-access-key-id`: AWS access key ID
  * `--aws-secret-access-key`: AWS secret access key
  * `--aws-account-id`: AWS account ID

* GCP OSD clusters:
  * `platform=gcp-osd`: Must pass in cluster parameters
  * `--gcp-service-account-file`: Path to GCP service account json.
     Follow [these](#steps-to-create-gcp-service-account-file) steps to get the ServiceAccount file.

### Cluster parameters
Every call to the openshift installer cli must have at least one `--cluster` option.  

* Mandatory parameters:
  * name: The name of the cluster
  * platform: The platform to deploy the cluster on (supported platforms are: aws, rosa and hypershift)
  * region: The region to deploy the cluster
* Optional parameters:
  * Parameter names should be separated by semicolons (`;`)
  * To set cluster create / destroy timeout (not applicable for AWS IPI clusters), pass `--cluster ...timeout=1h'`; default is 60 minutes.
  * `timeout` and `expiration-time` format examples: `1h`, `30m`, `3600s`
  * `ocm-env`: OCM environment to deploy the cluster; available options: `stage` or `production` (defaults to `stage`). AWS-IPI clusters only use `production`.
  * AWS/GCP IPI:
    * To overwrite cluster config, check below manifests for parameters
      * [aws-install-config-template.j2](openshift_cli_installer/manifests/aws-install-config-template.j2)
      * [gcp-install-config-template.j2](openshift_cli_installer/manifests/gcp-install-config-template.j2)
    * Every parameter (marked with double curly brackets in the template) can be overwritten.
    * For example: to overwrite `{{ fips|default("false", true) }}` pass `--cluster '...fips=true'`
  * ROSA / Hypershift:
    * Every supported ROSA CLI create/delete parameter can be passed. Check `rosa create --help` for more details.
    * For example:
      * Pass `--cluster ...fips=true'` to enable FIPS
      * Pass `--cluster ...expiration-time=2h'` to have the cluster expiration time set to 2 hours
  * Hypershift:
    * Cluster VPC CIDR, public and private subnets can be configured from the CLI. Otherwise, values in [setup-vpc.tf](openshift_cli_installer/manifests/setup-vpc.tf) will be used.
      * To set `cidr`, pass `--cluster ...cidr=1.1.0.0/16'`
      * To set `private-subnets`, pass `--cluster ...private-subnets=10.1.1.0/24,10.1.2.0/24'`
      * To set `public-subnets`, pass `--cluster ...public-subnets=10.1.10.0/24,10.1.20.0/24'`

#### Steps to create GCP Service Account File
 To create the file, create a service account and download it:  
 1. Go to https://console.cloud.google.com/iam-admin/serviceaccounts?project=<project>
 2. Select the service account -> "Create Key"
 3. Select the Key Type as `JSON` and click Create

### ACM (Advanced Cluster Management)
Managed clusters (Rosa, AWS and OSD) can be deployed with ACM and attached to ACM hub.
To deploy ACM on cluster pass `--cluster ... acm=True`
To enable observability on the ACM enabled cluster pass `--cluster ... acm-observability=True`
Attach clusters to ACM cluster hub:
  * Support only clusters that created during the run
  * To attach cluster to this ACM hub pass `--cluster ... acm-clusters=mycluser1,mycluster2`
    * `mycluser1,mycluster2` needs to be sent with `--cluster ...` for the script to create them.

### Destroy clusters

`--destroy-clusters-from-install-data-directory`, `--destroy-clusters-from-s3-bucket` and `--destroy-clusters-from-install-data-directory-using-s3-bucket` must have:

```bash
  --ocm-token=$OCM_TOKEN \
  --registry-config-file=${HOME}/docker-secrets.json \
  --aws-access-key-id=${AWS_ACCESS_KEY_ID} \
  --aws-secret-access-key=${AWS_SECRET_ACCESS_KEY}
```

## Destroy clusters from clusters data directory

To destroy all clusters locate in `--clusters-install-data-directory` run:

```bash
podman run quay.io/redhat_msi/openshift-cli-installer \
  --destroy-clusters-from-install-data-directory \
  --clusters-install-data-directory=/openshift-cli-installer/clusters-install-data
```

## Destroy clusters from clusters data directory using s3 bucket stored in cluster_data.yaml
```bash
podman run quay.io/redhat_msi/openshift-cli-installer \
  --destroy-clusters-from-install-data-directory-using-s3-bucket \
  --clusters-install-data-directory=/openshift-cli-installer/clusters-install-data
```

## Destroy clusters from S3 bucket
To destroy all clusters from uploaded zip files in S3 bucket run:

```bash
podman run quay.io/redhat_msi/openshift-cli-installer \
  --destroy-clusters-from-s3-bucket \
  --s3-bucket-name="openshift-cli-installer" \
  --s3-bucket-path="openshift-ci"
```

To filter cluster pass `--destroy-clusters-from-s3-bucket-query` query:
```bash
podman run quay.io/redhat_msi/openshift-cli-installer--destroy-clusters-from-s3-bucket \
  --s3-bucket-name=openshift-cli-installer \
  --s3-bucket-path=install-folders \
  --destroy-clusters-from-s3-bucket-query="mycluster"
```

### Usages

```
podman run quay.io/redhat_msi/openshift-cli-installer --help
```

### Local run

Clone the [repository](https://github.com/RedHatQE/openshift-cli-installer)

```
git clone https://github.com/RedHatQE/openshift-cli-installer.git
```

Install [poetry](https://github.com/python-poetry/poetry)

Install [regctl](https://github.com/regclient/regclient)

Install Terraform [how-to](https://computingforgeeks.com/how-to-install-terraform-on-fedora/)
```bash
sudo dnf config-manager --add-repo https://rpm.releases.hashicorp.com/fedora/hashicorp.repo
sudo dnf install terraform
```


Use `poetry run python openshift_cli_installer/cli.py` to execute the cli.

```
poetry install
poetry run python openshift_cli_installer/cli.py --help
```


### Create Clusters

Each command can be run via container `podman run quay.io/redhat_msi/openshift-cli-installer` or via poetry command `poetry run python openshift_cli_installer/cli.py`
When using the container pass:
`-e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID`
`-e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY`
`-v registry-config.json:/registry-config.json`
`-v ./clusters-install-data:/openshift-cli-installer/clusters-install-data`

#### One cluster

##### AWS/GCP IPI cluster

###### Versions
  * Supported `streams` are: `stable`, `nightly`, `rc`, `ci` and `ec`, Supported architecture(s): `X86_64`
  * If passed exact version this version will be used (if exists), Example: 3.14.9
  * If passed partial version, latest version will be used, Example: 4.13 install 4.13.9 (latest)
  * If passed `stream=nightly` and version 4.13, latest 4.13 nightly will be used.
    * stream should be passed as part on `--cluster`, `--cluster ...... stream=stable`

```
podman run quay.io/redhat_msi/openshift-cli-installer \
    --action create \
    --registry-config-file=registry-config.json \
    --s3-bucket-name=openshift-cli-installer \
    --s3-bucket-path=install-folders \
    --cluster 'name=ipi1;base-domain=aws.interop.ccitredhat.com;platform=aws;region=us-east-2;version=4.14.0-ec.2;worker-flavor=m5.xlarge;log_level=info'
```
  * Default `log_level=error` is set for cluster config to hide the openshift-installer logs which contains kubeadmin password.

##### ROSA cluster

###### Versions
  [Same for Hypershift clusters]

  * Supported `channel-group` are: `stable`, `candidate`, and `nightly`.
  * If passed exact version this version will be used (if exists), Example: 3.14.9
  * If passed partial version, latest version will be used, Example: 4.13 install 4.13.9 (latest)
  * If passed `channel-group=nightly` and version 4.13, latest 4.13 nightly will be used.
    * stream should be passed as part on `--cluster`, `--cluster ...... channel-group=stable`

```
podman run quay.io/redhat_msi/openshift-cli-installer \
    --action create \
    --ocm-token=$OCM_TOKEN \
    --cluster 'name=rosa1;platform=rosa;region=us-east-2;version=4.13.4;compute-machine-type=m5.xlarge;replicas=2;channel-group=candidate;expiration-time=4h;timeout=1h;ocm-env=production
```

##### Hypershift cluster

```
podman run quay.io/redhat_msi/openshift-cli-installer \
    --action create \
    --ocm-token=$OCM_TOKEN \
    --cluster 'name=hyper;platform=hypershift;region=us-west-2;version=4.13.4;compute-machine-type=m5.4xlarge;replicas=6;channel-group=candidate;expiration-time=4h;timeout=1h'
```

##### Multiple clusters

To run multiple clusters deployments in parallel pass -p,--parallel.

```
podman run quay.io/redhat_msi/openshift-cli-installer \
    --action create \
    --registry-config-file=registry-config.json \
    --s3-bucket-name=openshift-cli-installer \
    --s3-bucket-path=install-folders \  --cluster 'name=hyper1;platform=hypershift;region=us-west-2;version=4.13.4;compute-machine-type=m5.4xlarge;replicas=6;channel-group=candidate;expiration-time=2h;timeout=1h' \
    --ocm-token=$OCM_TOKEN \

    --cluster 'name=ipi1;base-domain=aws.interop.ccitredhat.com;platform=aws;region=us-east-2;version=4.14.0-ec.2;worker-flavor=m5.xlarge' \
    --cluster 'name=rosa1;platform=rosa;region=us-east-2;version=4.13.4;compute-machine-type=m5.xlarge;replicas=2;channel-group=candidate;expiration-time=4h;timeout=1h' \
    --parallel
```

### Destroy Clusters
#### One cluster
##### AWS IPI cluster

```
podman run quay.io/redhat_msi/openshift-cli-installer \
    --action destroy \
    --ocm-token=$OCM_TOKEN \
    --cluster 'name=ipi1;region=us-east-2;version=4.14.0-ec.2;timeout=1h'
```

##### ROSA cluster

```
podman run quay.io/redhat_msi/openshift-cli-installer \
    --action destroy \
    --ocm-token=$OCM_TOKEN \
    --cluster 'name=rosa1;platform=rosa;region=us-east-2;version=4.13.4;timeout=1h;ocm-env=production'
```


##### Hypershift cluster

```
podman run quay.io/redhat_msi/openshift-cli-installer \
    --action destroy \
    --ocm-token=$OCM_TOKEN \
    --cluster 'name=hyper1;platform=hypershift;region=us-east-2;version=4.13.4;timeout=1h'
```

##### Multiple clusters

To run multiple clusters deletion in parallel pass -p,--parallel.

```
podman run quay.io/redhat_msi/openshift-cli-installer \
    --action destroy \
    --ocm-token=$OCM_TOKEN \
    --cluster 'name=rosa1;platform=rosa;region=us-east-2;version=4.13.4;timeout=1h' \
    --cluster 'name=hyper1;platform=rosa;region=us-east-2;version=4.13.4;timeout=1h' \
    --cluster 'name=ipi1;region=us-east-2;version=4.14.0-ec.2;timeout=1h'
```
