#!/usr/bin/env python
"""
Name: mass-asg-rebuild
Author: shane.warner@fox.com
Synopsis: This tool automatically locates autoscaling enabled clusters in ASGARD, builds and bootstraps fresh nodes for them
via Chef, and creates AMI images for the resulting node builds for use with ASGARD and autoscaling groups.
"""
import chef
import boto
import sys
import time
import pprint
from datetime import datetime
from boto.ec2.blockdevicemapping import BlockDeviceType, BlockDeviceMapping

# Globals
imageId='ami-5d3d1f34'
failed_ids = []

class asg(object):
    def __init__(self):
        self.api = chef.autoconfigure()
        self.bag = chef.DataBag('clusters')
        try:
            self.ec2 = boto.connect_ec2()
        except Exception as e:
            print e
        self.threshold = 2400

    def cleanup(self):
        """
        Deletes the build nodes and clients out of the Chef server.
        """

        for row in chef.Search('node', 'name:*.internal'):
            node = chef.Node(row.object.name)
            chef.Node.delete(node)

    def stop_servers(self, instance_ids):
        """
        Stops instances specified in list. It will also stop any servers listed in failed_ids as a cleanup measure.
        :param instances_ids: List of instance ids to stop
        """

        status = 0
        stopped = []

        print "-------------------------------"
        print "Stopping instances"
        print "-------------------------------"

        for instance_id in instance_ids:
            try:
                self.ec2.create_tags(instance_id, {"Name": "chef-autobuild"})
            except Exception as e:
                print "Failed to add tag for {0}".format(instance_id)

            time.sleep(1)

            try:
                self.ec2.stop_instances(instance_id.encode('ascii'))
            except Exception as e:
                print "Failed to issue stop command for {0}".format(instance_id.encode('ascii'))
                print e

        try:
            reservations = self.ec2.get_all_reservations(filters={'reservation_id':failed_ids})
        except Exception as e:
            print "Failed to get instance reservations."
            print e

        instances = [i for r in reservations for i in r.instances]

        for instance in instances:
            try:
                self.ec2.create_tags(instance.id, {"Name": "chef-autobuild"})
            except Exception as e:
                print "Failed to add tag for {0}".format(instance.id)

                time.sleep(1)

            try:
                self.ec2.stop_instances(instance.id)
            except Exception as e:
                print "Failed to issue stop command for {0}".format(instance.id)
                print e

        while status == 0:
            time.sleep(10)
            for instance_id in instance_ids:
                reservations = self.ec2.get_all_instances(instance_ids=[instance_id])
                instance = reservations[0].instances[0]
                if instance.update() == 'stopped':
                    print instance_id + " stopped."
                    instance_ids.remove(instance_id)
                    stopped.append((instance_id))

            if not instance_ids:
                status=1

        return stopped

    def build_list(self):
        """
        Builds a list of cluster data for autoscaling clusters.
        We query the Chef server for nodes with autoscaling enabled and add their properties to the list to be
        returned.
        :return:
        """

        cluster_data = []

        print "Using AMI ID: {0}".format(imageId)
        print "-------------------------------"
        print "Identifying autoscale clusters"
        print "-------------------------------"

        for name, item in self.bag.iteritems():
            for row in chef.Search('node', 'cluster:' + name + " AND chef_environment:prod NOT cluster:splunk"):
                node = chef.Node(row.object.name)
                str = node['ec2']['userdata']

                if str is not None and len(str) > 0:
                    pos = str.find('CLOUD_STACK=autoscale')
                    if pos >= 1:
                        roles = node['roles'][0]
                        if len(node['roles']) == 3:
                            for role in node['roles']:
                                if role != "base" and role != "lamp-afs":
                                    roles = role
                                    break
                        elif len(node['roles']) == 2:
                            for role in node['roles']:
                                if role != "base" and role != "lamp":
                                    roles = role
                                    break

                        print "{0}".format(name)
                        cluster_data.append((name, "stage", roles, node['ec2']['security_groups']))
                        break

        return cluster_data

    def build_servers(self, cluster_data):
        """
        Bootstraps and builds autoscaling servers to be used for AMI imaging.
        :param cluster_data: List of the following format: [(name,env,roles,securityGroups),]
        """

        reservation_ids = []
        instance_ids = []
        status = 0
        now = time.time()
        timelimit = now + self.threshold
        map = BlockDeviceMapping()
        eph0 = BlockDeviceType()
        eph1 = BlockDeviceType()
        eph0.ephemeral_name = 'ephemeral0'
        eph1.ephemeral_name = 'ephemeral1'
        map['/dev/sdb'] = eph0
        map['/dev/sdc'] = eph1

        print "-------------------------------"
        print "Launching Chef builds"
        print "-------------------------------"

        for cluster, env, role, securityGroups in cluster_data:
            time.sleep(1)
            userData = 'HOSTNAME=chef-autobuild01 ENV=stage CLUSTER=' + cluster + ' AUTOSCALE=1 AUTOBUILD=1 ROLES=' + role

            try:
                reservation = self.ec2.run_instances(image_id=imageId, key_name='ffe-ec2', security_groups=securityGroups,
                                            instance_type='c3.xlarge', user_data=userData, block_device_map=map)
                print "Launched " + reservation.id
                reservation_ids.append((reservation.id))
            except Exception as e:
                print "Failed to launch instance for cluster: {0}".format(cluster)
                print e

        print "-------------------------------"
        print "Waiting for builds to complete"
        print "-------------------------------"
        while status == 0:
            time.sleep(10)

            if time.time() >= timelimit:
                for r_id in reservation_ids:
                    reservation_ids.remove(r_id)
                    failed_ids.append(r_id)

            for r_id in reservation_ids:
                for row in chef.Search('node', 'ec2_reservation_id:' + r_id + " AND chef_environment:stage", 1):
                    node = chef.Node(row.object.name)
                    if node is not None and len(row) > 0:
                        print node['ec2']['instance_id'] + " => " + node['cluster']
                        reservation_ids.remove(r_id)
                        instance_ids.append((node['ec2']['instance_id']))

            if not reservation_ids:
                status=1

        return instance_ids

    def create_images(self, stopped):
        """
        Creates AMI images for the specified instances.
        :param stopped: List of instances in the stopped state.
        """

        completed = []
        ami_ids = []
        status = 0
        now = time.time()
        timelimit = now + self.threshold

        print "-------------------------------"
        print "Starting AMI imaging..."
        print "-------------------------------"

        timestamp = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
        for instance_id in stopped:
            for row in chef.Search('node', 'ec2_instance_id:' + instance_id + " AND chef_environment:stage", 1):
                time.sleep(1)
                node = chef.Node(row.object.name)
                if node is not None and len(row) > 0:
                    try:
                        ami = self.ec2.create_image(instance_id.encode('ascii'),node['cluster'] + "-autoscale-" + timestamp)
                        ami_ids.append((ami, node['cluster']))
                    except Exception as e:
                        print "Failed to issue create_image for {0}".format(instance_id)
                        print e

        while status == 0:
            for ami, cluster in ami_ids:
                time.sleep(5)
                ami_status = self.ec2.get_image(ami.encode('ascii'))
                ami_status.update
                if ami_status.state == 'available':
                    print ami + " completed."
                    completed.append((ami, cluster))
                    ami_ids.remove((ami, cluster))

            if time.time() >= timelimit:
                for ami, cluster in ami_ids:
                    ami_ids.remove((ami, cluster))
                    failed_ids.append(ami)

            if not ami_ids:
                status=1

        return completed

def main():
    autoscale = asg()
    cluster_data = autoscale.build_list()
    instance_ids = autoscale.build_servers(cluster_data)
    stopped = autoscale.stop_servers(instance_ids)
    completed = autoscale.create_images(stopped)
    autoscale.cleanup()

    print "Run complete."
    print "SUMMARY:"
    print "-------------------------------"
    print "     AMI => CLUSTER            "
    print "-------------------------------"
    for ami, cluster in completed:
        print ami + " => " + cluster

    print "-------------------------------"
    print "FAILED BUILDS/AMIS"
    print "-------------------------------"
    for failed_id in failed_ids:
        print failed_id

    print "-------------------------------"

if __name__ == "__main__":
    main()
