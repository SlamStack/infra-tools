#!/usr/bin/env python
"""
Toolset for working with KMS encrypted Chef databags
"""

__author__ = 'shane.warner@fox.com'

import argparse
import boto
import base64
import chef
import json
import time
import os
import subprocess
from subprocess import call
from argparse import RawTextHelpFormatter
from boto import utils
from boto.s3.key import Key

class kmsdb(object):
    def __init__(self):
        try:
            # Get the account id
            response = boto.utils.get_instance_identity()
            self.acct_id = response.get("document")['accountId']
            self.kms = boto.connect_kms()
            self.iam = boto.connect_iam()
            self.s3 = boto.connect_s3()
            self.api = chef.autoconfigure()
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

    def create(self, name):
        """
        :param name:  Name of the databag object to be created. The name will also be used for the KMS master key.
        :return: True or False
        """

        # Check if the databag file already exists
        if self.databag_exists(name):
            print "[-] Looks like that data bag already exists. Try the edit function instead."
            return

        # Let's create an IAM role for the specified databag to be created. (In most cases this is a cluster name)
        # If we find that the role already exists we'll move forward and use it.
        try:
            response = self.iam.create_role(name)
        except Exception as e:
            if e.code == "EntityAlreadyExists":
                client_role_arn = "arn:aws:iam::" + self.acct_id + ":role/" + name
                print "[+] Using existing role {0}".format(client_role_arn)
                self.recycle_role = 1

        if self.recycle_role == 0:
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

            print "[+] Created role {0}".format(client_role_arn)

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

        data_key = response[u'Plaintext']
        ciphertextblob = response[u'CiphertextBlob']

        # Store the databag name, and CiphertextBlob in the master secrets file. in json format with base64 key
        base64_ciphertextblob = base64.b64encode(ciphertextblob)
        json_data = {name.replace("alias/cluster/", ""): {'CiphertextBlob': base64_ciphertextblob}}

        try:
            with open(self.__secrets_dir__ + name.replace("alias/cluster/", "") + ".json", 'w') as outfile:
                json.dump(json_data, outfile)
        except:
            print "[-] Error writing to secrets file {0}".format(self.__secrets_dir__ + name.replace("alias/cluster/", "") + ".json")
            return

        print "[+] Wrote to secrets file {0}".format(self.__secrets_dir__ + name.replace("alias/cluster/", "") + ".json")

        # Upload the secrets file to the s3 bucket
        self.upload_to_s3(name.replace("alias/cluster/", ""), self.__secrets_dir__ + name.replace("alias/cluster/", "") + ".json")

        # Store the actual key in a temp file on /dev/shm for use in creating the encrypted databag
        try:
            f = open('/dev/shm/tmp_data_key', 'w')
            f.write(base64.b64encode(data_key))
            f.close()
        except:
            print "[-] Error writing temp data key file on /dev/shm."
            return

        # Create the encrypted data bag using knife (for now)
        try:
            subprocess.call("knife data bag create " + name.replace("alias/cluster/", "") + " --secret-file /dev/shm/temp_data_key", shell=True)
        except OSError as e:
            print "[-] Error creating Chef data bag."
            print e

        # Populate the data bag with the "secrets" id
        f = "/dev/shm/json_load.json"
        json_data = {"id": "secrets"}

        try:
            with open(f, 'w') as outfile:
                json.dump(json_data, outfile)
        except:
            print "[-] Error writing to temp json load file {0}".format(f)
            return

        try:
            subprocess.call("knife data bag from file " + name.replace("alias/cluster/", "") + " " + f + " -s /dev/shm/temp_data_key", shell=True)
        except OSError as e:
            print "[-] Error creating Chef data bag."
            print e

        # Securely delete the json load file
        self.secure_delete(f, passes=10)

        # Securely remove the data key from ramdisk.
        self.secure_delete("/dev/shm/tmp_data_key", 10)

    def databag_exists(self, name):
        """
        Checks whether a databag exists or not.
        :param name: Name of the databag
        :return: Returns True or False
        """
        try:
            bag = chef.DataBag(name)
        except Exception as e:
            return False

        if bag.exists:
            return True

        return False

    def delete(self, name):
        return

    def edit(self, name):
        # Check if the data bag even exists before proceeding.
        if not self.databag_exists(name):
            print "[-] Looks like that data bag doesn't exist. Try creating with the create function first."
            return

        decrypted_key = self._get_data_key(name)

        # store the key in a temporary file in /dev/shm for working with the databag
        key_file = "/dev/shm/" + name + ".tmp.key"
        try:
            file = open(key_file, "w")
        except Exception as e:
            print "[-] Error creating temp data key file {0}".format(key_file)
            return

        file.write(base64.b64encode(decrypted_key))
        file.close()

        # download the Chef databag in json format (decrypted) to a temp file on /dev/shm for editing
        databag_file =  "/dev/shm/" + name + ".json"
        try:
            subprocess.call("knife data bag show " + name + " secrets -Fj --secret-file /dev/shm/" + name + ".tmp.key >" + databag_file, shell=True)
        except OSError as e:
            print "[-] Error dumping Chef data bag."
            print e

        # Call $EDITOR to edit the json file.
        EDITOR = os.environ.get('EDITOR','vim')
        call([EDITOR, databag_file])

        # Upload the databag file back to chef
        try:
            subprocess.call("knife data bag from file " + name + " " + databag_file + " -s " + key_file, shell=True)
        except OSError as e:
            print "[-] Error uploading Chef data bag."
            print e

        # Get rid of the evidence
        self.secure_delete(databag_file, passes=10)
        self.secure_delete(key_file, passes=10)

        return

    def upload_to_s3(self, name, file_name):
        """
        Uploads the json file to an s3 bucket
        :param name: Name of the cluster
        :param file_name: Full path to the local file name
        :return:
        """
        f = open(file_name, "r")
        path = "cluster/" + name + "/"
        bucket = self.s3.get_bucket(self.__secrets_bucket__)
        k = Key(bucket)
        full_object_name = path + "secrets.json"
        k.name = full_object_name

        try:
            k.set_contents_from_file(f)
        except Exception as e:
            print "[-] Error uploading json to s3"
            print e

        print "[+] Uploaded {0}".format("s3://" + self.__secrets_bucket__ + "/" + full_object_name)

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
    parser = argparse.ArgumentParser(description='kmsdb.py provides a toolset for working with KMS encrypted Chef databags.',
                                     formatter_class=RawTextHelpFormatter)
    subparsers = parser.add_subparsers(title='operations', help='Available operations')

    create_parser = subparsers.add_parser('create', help='Create a new KMS encrypted Chef databag.')
    create_parser.set_defaults(operation='create')
    create_parser.add_argument('--name', help='Name of the databag. In most cases this will be the name of the related cluster.',
                               required=True)

    edit_parser = subparsers.add_parser('edit', help='Edit a databag.')
    edit_parser.set_defaults(operation='edit')
    edit_parser.add_argument('--name', help='Databag to edit.', required=True)

    args = vars(parser.parse_args())

    db = kmsdb()

    if args['operation'] == "create":
        db.create(args['name'])

    if args['operation'] == "edit":
        db.edit(args['name'])

if __name__ == "__main__":
    main()
