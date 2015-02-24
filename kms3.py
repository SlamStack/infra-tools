#!/usr/bin/env python
"""
Toolset for working with:
 - KMS encrypted files on s3.
 - Roles, grants and KMS keys on a per cluster context.
"""

__author__ = 'shane.warner@fox.com'

import argparse
import boto
import base64
import chef
import json
import time
import os
import random
import subprocess
from subprocess import call
from argparse import RawTextHelpFormatter
from boto import utils
from boto.s3.key import Key
from Crypto.Cipher import AES

class kms3(object):
    def __init__(self):
        try:
            # Get the account id
            response = boto.utils.get_instance_identity()
            self.acct_id = response.get("document")['accountId']
            self.kms = boto.connect_kms()
            self.iam = boto.connect_iam()
            self.s3 = boto.connect_s3()
            self.__secrets_dir__ = "/root/.chef/secrets/"
            self.__secrets_bucket__ = "ffe-secrets"
            self.__chef_role_arn__ = "arn:aws:iam::" + self.acct_id + ":role/chef"
            self.__key_spec__ = "AES_256"
            self.recycle_key = 0
            self.recycle_role = 0
        except Exception as e:
            print "[-] Error:"
            print e
            return

    def pad(self, s):
        return s + b"\0" * (AES.block_size - len(s) % AES.block_size)

    def encrypt(self, message, key, key_size=256):
        message = self.pad(message)
        iv = ''.join(chr(random.randint(0, 0xFF)) for i in range(AES.block_size))
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return iv + cipher.encrypt(message)

    def decrypt(self, ciphertext, key):
        iv = ciphertext[:AES.block_size]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        plaintext = cipher.decrypt(ciphertext[AES.block_size:])
        return plaintext.rstrip(b"\0")

    def encrypt_file(self, file_name, key):
        with open(file_name, 'rb') as fo:
            try:
                plaintext = fo.read()
            except:
                print "[-] Error opening file {0} for reading.".format(file_name)
                return

        enc = self.encrypt(plaintext, key)
        with open("/dev/shm/" + os.path.basename(file_name) + ".enc", 'wb') as fo:
            try:
                fo.write(enc)
            except:
                print "[-] Error writing tmp file {0}".format("/dev/shm/" + os.path.basename(file_name) + ".enc")

        return

    def decrypt_file(self, file_name, key):
        with open(file_name, 'rb') as fo:
            ciphertext = fo.read()
        dec = self.decrypt(ciphertext, key)
        with open(file_name[:-4], 'wb') as fo:
            fo.write(dec)

    def download_from_s3(self, cluster, file_name):
        """
        Downloads the specified file name from the cluster's s3 bucket/prefix.
        :param cluster: Name of the cluster the file belongs to.
        :param file_name: File name on s3.
        :return:
        """
        return

    def download_data_key(self, name):
        """
        Downloads a cluster's data key to a temp file on /dev/shm
        :param name: Cluster name.
        :return:
        """
        temp_data_key = self._get_data_key(name)
        output_file = "/dev/shm/" + name + ".tmp.key"

        try:
            file = open(output_file, "w")
        except Exception as e:
            print "[-] Error opening /dev/shm for writing."
            return

        file.write(temp_data_key)
        print "[+] {0} data key saved to {1}".format(name, output_file)

    def edit(self, cluster, name):
        """
        Edits a cluster specific file on s3.
        :param name: File name.
        :param cluster: Name of the cluster the file belongs to.
        :return:
        """
        # Grab the data key from IAM
        decrypted_key = self._get_data_key(name)

        # store the key in a temporary file in /dev/shm for working with the encrypted file.
        key_file = "/dev/shm/" + name + ".tmp.key"
        try:
            file = open(key_file, "w")
        except Exception as e:
            print "[-] Error creating temp data key file {0}".format(key_file)
            return

        file.write(decrypted_key)
        file.close()

        # Download the file from s3 to /dev/shm

        # Call $EDITOR to edit the file.
        EDITOR = os.environ.get('EDITOR','vim')
        call([EDITOR, databag_file])

        # Upload the file back to s3


        # Get rid of the evidence
        # Nuke file here
        self.secure_delete(key_file, passes=10)

        return

    def exists_on_s3(self, name, file_name):
        """
        Checks for the existence of a file on s3.
        :param name: Name of the cluster the file belongs to.
        :param file_name: Name of the file on s3.
        :return:
        """
        path = "cluster/" + name + "/" + file_name
        bucket = self.s3.get_bucket(self.__secrets_bucket__)

        try:
            response = bucket.get_key(path)
        except Exception as e:
            print "[-] Error"
            print e
            return

        if response:
            return True

        return False

    def secure_delete(self, path, passes=1):
        """
        :param path: Path to object to securely wipe
        :param passes: Number of passes
        :return:
        """
        try:
            retcode = subprocess.call("shred -u -n " + str(passes) + " " + path, shell=True)
        except OSError as e:
            print "[-] Error shredding temp data key file {0}".format(path)
            print e
            return

        return

    def setup(self, name):
        """
        :param name:  Name of the cluster to be created. The name will also be used for the KMS master key.
        :return: True or False
        """

        # Check for the existance of a secrets file for this cluster before proceeding.
        # This is a good indication that the setup process has already been completed.
        if self.exists_on_s3(name, name + ".json"):
            print "[-] Looks like that cluster has already been setup."
            return

        # Let's create an IAM role for the specified cluster to be created.
        # If we find that the role already exists we'll move forward and use it.
        try:
            response = self.iam.create_role(name)
        except Exception as e:
            if e.code == "EntityAlreadyExists":
                client_role_arn = "arn:aws:iam::" + self.acct_id + ":role/" + name
                print "[+] Using existing role {0}".format(client_role_arn)
                self.recycle_role = 1

        if self.recycle_role == 0:
            # The role does not exist so we will create it along with an instance profile.
            client_role_arn = response.get("create_role_response")[u'create_role_result'].arn
            try:
                response = self.iam.create_instance_profile(name)
            except Exception as e:
                print "[-] Error creating instance profile {0}".format(name)
                print e

            try:
                self.iam.add_role_to_instance_profile(name, name)
            except Exception as e:
                print "[-] Error adding role {0} to instance_profile {1}".format(name, name)

            # Since the role did not previously exist. We can safely create the policy for the cluster s3 prefix and
            # apply it to the role
            role_policy = """{
                  "Statement": [
                    {
                      "Action": [
                        "s3:GetObject"
                      ],
                      "Effect": "Allow",
                      "Resource": [
                        "arn:aws:s3:::ffe-secrets/cluster",
                        "arn:aws:s3:::ffe-secrets/cluster/""" + name + """/*"
                      ]
                    }
                  ]
                }"""

            try:
                response = self.iam.put_role_policy(name, name + "-s3-secrets-access", role_policy)
            except Exception as e:
                print "[-] Error"
                print e
                return

            print "[+] Created role {0} and applied policy".format(client_role_arn)

        # Convert name into a format AWS prefers
        name = 'alias/cluster/' + name

        # We'll see if an alias already exists and re-use the existing key. If not we'll create a new one.
        response = self.kms.list_aliases()
        for alias in response.get("Aliases"):
            if name == alias[u'AliasName']:
                self.recycle_key = 1
                key_id = alias[u'TargetKeyId']
                print "[+] Using existing master key {0}".format(key_id)
                time.sleep(2)
                break

        if self.recycle_key == 0:
            # First we need to create the master key in KMS
            try:
                response = self.kms.create_key()
            except Exception as e:
                print "[-] Error:"
                print e
                return

            key_id = response.get("KeyMetadata")[u'KeyId']
            print "[+] Created master key {0}".format(key_id)
            time.sleep(6)

            # Let's create an alias based on "name" and assign it to the master key we just created.
            try:
               self.kms.create_alias(name, key_id)
            except Exception as e:
                print "[-] Error:"
                print e
                return
        else:
            time.sleep(10)

        # We'll create the grants on the key we just created.
        # Chef will get encrypt/decrypt and others needed. The client role will get decrypt only.
        try:
            self.kms.create_grant(key_id, self.__chef_role_arn__, operations=["Encrypt", "Decrypt", "GenerateDataKey", "ReEncryptFrom",
                                                                         "ReEncryptTo", "CreateGrant"])
        except Exception as e:
            print "[-] Error:"
            print e
            return
        try:
            self.kms.create_grant(key_id, client_role_arn, operations=["Decrypt"])
        except Exception as e:
            print "[-] Error:"
            print e
            return

        # Next we'll need to generate a data key to use for encrypting our Chef databag. We'll also want to store the
        # cipherBlob returned from the API in our secrets master file.
        try:
            response = self.kms.generate_data_key(key_id, key_spec=self.__key_spec__)
        except Exception as e:
            print "[-] Error:"
            print e
            return

        ciphertextblob = response[u'CiphertextBlob']

        # Store the cluster name, and CiphertextBlob in the master secrets file. in json format with base64 key
        base64_ciphertextblob = base64.b64encode(ciphertextblob)
        json_data = {name.replace("alias/cluster/", ""): {'CiphertextBlob': base64_ciphertextblob}}

        try:
            with open(self.__secrets_dir__ + name.replace("alias/cluster/", "") + ".json", 'w') as outfile:
                json.dump(json_data, outfile)
        except:
            print "[-] Error writing to secrets file {0}".format(self.__secrets_dir__ + name.replace("alias/cluster/", "") + ".json")
            return

        print "[+] Wrote secrets to master cluster file {0}".format(self.__secrets_dir__ + name.replace("alias/cluster/", "") + ".json")

        # Upload the secrets file to the s3 bucket
        self.upload_to_s3(name.replace("alias/cluster/", ""), self.__secrets_dir__ + name.replace("alias/cluster/", "") + ".json")

    def upload(self, name, file_name):
        """
        Encrypts and uploads a file to a cluster's bucket/prefix on s3.
        :param name: Name of the cluster the file belongs to.
        :param file_name: Name of the file to upload.
        :return:
        """
        # Get the cluster's data key from KMS
        temp_data_key = self._get_data_key(name)

        if temp_data_key:
            # AES-256 encrypt the file
            self.encrypt_file(file_name, temp_data_key)

            # Upload the file to s3
            self.upload_to_s3(name, "/dev/shm/" + os.path.basename(file_name) + ".enc")

            # Remove the file from /dev/shm securely
            self.secure_delete("/dev/shm/" + os.path.basename(file_name) + ".enc", 10)

        return

    def upload_to_s3(self, name, file_name):
        """
        Uploads file to an s3 bucket
        :param name: Name of the cluster.
        :param file_name: Full path to the local file name.
        :return:
        """
        f = open(file_name, "r")
        path = "cluster/" + name + "/" + os.path.basename(file_name)
        bucket = self.s3.get_bucket(self.__secrets_bucket__)
        k = Key(bucket)
        k.name = path

        try:
            k.set_contents_from_file(f)
        except Exception as e:
            print "[-] Error uploading file to s3"
            print e

        print "[+] Uploaded {0}".format("s3://" + self.__secrets_bucket__ + "/" + path)

        return

    def _get_data_key(self, name):
        """
        Internal function to retrieve the data key for a cluster using KMS and the secrets file.
        :param name:  Name of the databag (Usually cluster name)
        :return:
        """
        # Load the ciphertext blob for the databag from the secrets file
        try:
            json_data = open(self.__secrets_dir__ + name + ".json", "r")
        except:
            print "[-] Error opening json file for {0}".format(name)
            return

        # Decode into raw form before passing to KMS
        data = json.load(json_data)
        ciphertextblob = base64.b64decode(data[name]["CiphertextBlob"])

        # Decrypt the data key ciphertext blob with KMS.
        try:
            response = self.kms.decrypt(ciphertext_blob=ciphertextblob)
        except Exception as e:
            print e
            return

        decrypted_key = response.get("Plaintext")

        return decrypted_key

def main():
    parser = argparse.ArgumentParser(description='kms3.py ',
                                     formatter_class=RawTextHelpFormatter)
    subparsers = parser.add_subparsers(title='operations', help='Available operations')

    edit_parser = subparsers.add_parser('edit', help='Edit an KMS encrypted file on s3. WARNING: This is not intended for binaries.')
    edit_parser.set_defaults(operation='edit')
    edit_parser.add_argument('--cluster', help='Name of cluster the file belongs to.', required=True)
    edit_parser.add_argument('--file', help='Name of the file.')

    create_parser = subparsers.add_parser('setup', help='Set up a new cluster for KMS file storage. Creates a role, data key, grants and s3 bucket prefix for the specified cluster.')
    create_parser.set_defaults(operation='setup')
    create_parser.add_argument('--name', help='Name of the cluster.',
                               required=True)

    create_parser = subparsers.add_parser('get-key', help='Downloads the data key for a cluster and stores it in /dev/shm.')
    create_parser.set_defaults(operation='get-key')
    create_parser.add_argument('--name', help='Name of the cluster.',
                               required=True)

    create_parser = subparsers.add_parser('upload', help='Encrypts and uploads the specified file to the specified cluster s3 bucket/prefix.')
    create_parser.set_defaults(operation='upload')
    create_parser.add_argument('--name', help='Name of the cluster.',
                               required=True)
    create_parser.add_argument('--file', help='Full path of the local file to upload.',
                                required=True)

    args = vars(parser.parse_args())

    api = kms3()

    if args['operation'] == "edit":
        api.edit(args['name'])
    if args['operation'] == "get-key":
        api.download_data_key(args['name'])
    if args['operation'] == "setup":
        api.setup(args['name'])
    if args['operation'] == "upload":
        api.upload(args['name'], args['file'])

if __name__ == "__main__":
    main()
