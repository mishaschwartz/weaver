#!/usr/bin/env python
__doc__ = """
Extracts and fetches NetCDF files from a Metalink file containing an URL, and outputs the NetCDF file at a given
index of the list.
"""
import argparse
import logging
import os
import sys
from tempfile import TemporaryDirectory

CUR_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, CUR_DIR)
# root to allow 'from weaver import <...>'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(CUR_DIR))))

# place weaver specific imports after sys path fixing to ensure they are found from external call
# pylint: disable=C0413,wrong-import-order
from weaver import WEAVER_ROOT_DIR, xml_util  # isort:skip # noqa: E402
from weaver.processes.builtin.utils import validate_file_reference  # isort:skip # noqa: E402
from weaver.utils import fetch_file  # isort:skip # noqa: E402

PACKAGE_NAME = os.path.split(os.path.splitext(__file__)[0])[-1]
PACKAGE_BASE = __file__.split(WEAVER_ROOT_DIR.rstrip("/") + "/")[-1].rsplit(PACKAGE_NAME)[0]
PACKAGE_MODULE = f"{PACKAGE_BASE}{PACKAGE_NAME}".replace("/", ".")

# setup logger since it is not run from the main 'weaver' app
LOGGER = logging.getLogger(PACKAGE_MODULE)
LOGGER.addHandler(logging.StreamHandler(sys.stdout))
LOGGER.setLevel(logging.INFO)

# process details
__version__ = "1.3"
__title__ = "Metalink to NetCDF"
__abstract__ = __doc__  # NOTE: '__doc__' is fetched directly, this is mostly to be informative


def m2n(metalink_reference, index, output_dir):
    # type: (str, int, str) -> None
    LOGGER.info(
        "Got arguments: metalink_reference=%s index=%s output_dir=%s", metalink_reference, index, output_dir
    )
    LOGGER.info("Process '%s' execution starting...", PACKAGE_NAME)
    LOGGER.info("Process '%s' output directory: [%s].", PACKAGE_NAME, output_dir)
    try:
        if not os.path.isdir(output_dir):
            raise ValueError(f"Output dir [{output_dir}] does not exist.")
        validate_file_reference(metalink_reference)
        with TemporaryDirectory(prefix=f"wps_process_{PACKAGE_NAME}_") as tmp_dir:
            LOGGER.info("Fetching Metalink file: [%s]", metalink_reference)
            metalink_path = fetch_file(metalink_reference, tmp_dir, timeout=10, retry=3)
            LOGGER.info("Reading Metalink file: [%s]", metalink_path)
            xml_data = xml_util.parse(metalink_path)
            LOGGER.info("Parsing Metalink file references.")
            nc_file_url = xml_data.xpath(f"string(//metalink/file[{index}]/metaurl)")
            LOGGER.info("Fetching NetCDF reference from Metalink file: [%s]", metalink_reference)
            LOGGER.info("NetCDF file URL : %s", nc_file_url)
            fetch_file(nc_file_url, output_dir)
    except Exception as exc:
        # log only debug for tracking, re-raise and actual error wil be logged by top process monitor
        LOGGER.info("Process '%s' raised an exception: [%s]", PACKAGE_NAME, exc)
        raise
    LOGGER.info("Process '%s' execution completed.", PACKAGE_NAME)


def main():
    LOGGER.info("Parsing inputs of '%s' process.", PACKAGE_NAME)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", metavar="metalink", type=str,
                        help="Metalink file to be parsed for NetCDF file names.")
    parser.add_argument("-n", metavar="index", type=int,
                        help="Index of the specific NetCDF file to extract. First element's index is 1.")
    parser.add_argument("-o", metavar="outdir", default=CUR_DIR,
                        help="Output directory of the retrieved NetCDF files extracted by name from the Metalink file.")
    args = parser.parse_args()
    sys.exit(m2n(args.i, args.n, args.o))


if __name__ == "__main__":
    main()
