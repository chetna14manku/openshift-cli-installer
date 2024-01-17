import os
import shutil
from pathlib import Path
import tempfile

from simple_logger.logger import get_logger
from openshift_cli_installer.utils.const import GCP_STR

LOGGER = get_logger(name=__name__)


def set_gcp_configuration(user_input):
    """
    Saves provided GCP Service Account file at location '~/.gcp/osServiceAccount.json'

    If file already exists at this path, it will be copied to tmp file first.

    Returns:
        dict: A dictionary of parameters needed for setting/restoring GCP Service Account file

    """
    gcp_params = {}
    if any([_cluster["platform"] == GCP_STR for _cluster in user_input.clusters]):
        gcp_sa_file_dir = os.path.join(os.path.expanduser("~"), ".gcp")
        openshift_installer_gcp_sa_file_path = os.path.join(gcp_sa_file_dir, "osServiceAccount.json")
        gcp_params["gcp_sa_file_dir"] = gcp_sa_file_dir
        gcp_params["openshift_installer_gcp_sa_file_path"] = openshift_installer_gcp_sa_file_path

        if os.path.exists(openshift_installer_gcp_sa_file_path):
            gcp_params["backup_existing_gcp_sa_file_path"] = tempfile.NamedTemporaryFile(suffix="-installer.json").name
            LOGGER.info(
                f"File {openshift_installer_gcp_sa_file_path} already exists. Copying to {gcp_params['backup_existing_gcp_sa_file_path']}"
            )
            shutil.copy(openshift_installer_gcp_sa_file_path, gcp_params["backup_existing_gcp_sa_file_path"])
        else:
            Path(gcp_sa_file_dir).mkdir(parents=True, exist_ok=True)
        LOGGER.info(f"Saving GCP ServiceAccount file to {openshift_installer_gcp_sa_file_path}")
        shutil.copy(user_input.gcp_service_account_file, openshift_installer_gcp_sa_file_path)

    return gcp_params


def restore_gcp_configuration(gcp_params):
    """
    Restores location '~/.gcp/osServiceAccount.json'

    Copy file from tmp location to '~/.gcp/osServiceAccount.json' if exists before,
    Otherwise removes the directory '~/.gcp'

    """
    if gcp_params:
        openshift_installer_gcp_sa_file_path = gcp_params["openshift_installer_gcp_sa_file_path"]
        if backup_gcp_sa_file := gcp_params.get("backup_existing_gcp_sa_file_path"):
            LOGGER.info(f"Restoring previous file contents of {openshift_installer_gcp_sa_file_path}")
            shutil.copy(backup_gcp_sa_file, openshift_installer_gcp_sa_file_path)
            os.remove(backup_gcp_sa_file)
        else:
            LOGGER.info(f"Deleting path {openshift_installer_gcp_sa_file_path}")
            shutil.rmtree(gcp_params["gcp_sa_file_dir"])
