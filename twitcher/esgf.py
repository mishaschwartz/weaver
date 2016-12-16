"""
Module :mod:`twitcher.esgf`
==========================

Retrieve an ESGF certificate using a esgf access token and
prepare a `.dodsrc` file for OpenDAP access to the ESGF data archive.

This module uses code from esgf-slcs-client-example_ and esgf-pyclient_.


.. _esgf-scls-client-example: https://github.com/cedadev/esgf-slcs-client-example
.. _esgf-pyclient: https://github.com/ESGF/esgf-pyclient

"""

import os
from OpenSSL import crypto
import base64
import requests
from requests_oauthlib import OAuth2Session

import logging
logger = logging.getLogger(__name__)


ROOT_PATH = '/tmp/'
ESGF_DIR = ROOT_PATH
ESGF_CERTS_DIR = 'certificates'
ESGF_CREDENTIALS = 'credentials.pem'
DAP_CONFIG = os.path.join(ROOT_PATH, '.dodsrc')
DAP_CONFIG_MARKER = '<<< Managed by twitcher >>>'

DAP_CONFIG_TEMPL = """\
{preamble}
# BEGIN {marker}
HTTP.VERBOSE={verbose}
HTTP.COOKIEJAR={esgf_dir}/.dods_cookies
HTTP.SSL.VALIDATE=0
HTTP.SSL.CERTIFICATE={esgf_dir}/credentials.pem
HTTP.SSL.KEY={esgf_dir}/credentials.pem
HTTP.SSL.CAPATH={esgf_certs_dir}
# END {marker}
{postamble}
"""


class ESGFAccessManager(object):
    def __init__(self, url, esgf_dir=None, dap_config=None):
        self.url = url
        self.esgf_dir = esgf_dir or ESGF_DIR
        self.esgf_credentials = os.path.join(self.esgf_dir, ESGF_CREDENTIALS)
        self.esgf_certs_dir = os.path.join(self.esgf_dir, ESGF_CERTS_DIR)
        self.dap_config = dap_config or DAP_CONFIG

        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

    def logon(self, access_token):
        cert = self._get_certificate(access_token)
        self._write_certificate(cert)
        self._write_dap_config()

    def _get_certificate(self, client_id, access_token):
        """
        Generates a new private key and certificate request, submits the request to be
        signed by the SLCS CA and returns the certificate.
        """
        # Generate a new key pair
        key_pair = crypto.PKey()
        key_pair.generate_key(crypto.TYPE_RSA, 2048)
        private_key = crypto.dump_privatekey(crypto.FILETYPE_PEM, key_pair).decode("utf-8")

        # Generate a certificate request using that key-pair
        cert_request = crypto.X509Req()

        # Create public key object
        cert_request.set_pubkey(key_pair)

        # Add the public key to the request
        cert_request.sign(key_pair, 'md5')
        der_cert_req = crypto.dump_certificate_request(crypto.FILETYPE_ASN1, cert_request)

        encoded_cert_req = base64.b64encode(der_cert_req)

        # Build the OAuth session object
        token = {'access_token': access_token, 'token_type': 'Bearer'}
        slcs = OAuth2Session(client_id, token=token)
        #headers = {}
        #headers['Authorization'] = 'Bearer %s' % access_token
        #post_data = urllib.urlencode({'certificate_request': encoded_cert_req})

        response = slcs.post(
            self.url,
            data={'certificate_request': encoded_cert_req},
            verify=False
        )

        # response = requests.post(self.url,
        #                          headers=headers,
        #                          data=post_data,
        #                          verify=False)
        if response.status_code == 200:
            content = "{} {}".format(response.text, private_key)
        else:
            msg = "Could not get certificate: {} {}".format(response.status_code, response.reason)
            raise Exception(msg)
        return content

    def _write_certificate(self, certificate):
        with open(self.esgf_credentials, 'w') as fh:
            fh.write(certificate)

    def _write_dap_config(self, verbose=False, validate=False):
        with open(self.dap_config, 'w') as fh:
            fh.write(DAP_CONFIG_TEMPL.format(verbose=1 if verbose else 0,
                                             validate=1 if validate else 0,
                                             esgf_certs_dir=self.esgf_certs_dir,
                                             esgf_dir=self.esgf_dir,
                                             marker=DAP_CONFIG_MARKER,
                                             preamble='',
                                             postamble='',
                                             ))
