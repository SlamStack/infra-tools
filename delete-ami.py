#!/usr/bin/env python
"""
Deletes supplied AMI and associated snapshot
"""
import boto
import sys

def deleteAmi(ami):
    ec2 = boto.connect_ec2()
    try:
        ec2.deregister_image(ami, delete_snapshot=True)
    except Exception as e:
        print "Failed to delete {0}".format(ami)
        print e.message

def main():
    if len(sys.argv) < 2:
        print "No AMI specified. Please specify an AMI ID"
        sys.exit(-1)

    ami = sys.argv[1]
    deleteAmi(ami)

if __name__ == '__main__':
    main()
