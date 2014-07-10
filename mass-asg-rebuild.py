#!/usr/bin/python
"""
Name: infra-tools
Author: shane.warner@fox.com
Synopsis: This script automatically locates autoscaling enabled clusters, builds and bootstraps fresh nodes for them
via Chef, and creates AMI images for the resulting node builds for use with asgard and autoscaling groups.
"""
import chef
import boto
import sys
import time
import pprint
from datetime import datetime

# Globals
imageId='ami-5d3d1f34'
failed_ids = []

class asg(object):
    def __init__(self):
        self.api = chef.autoconfigure()
        self.bag = chef.DataBag('clusters')
        self.ec2 = boto.connect_ec2()
        self.threshold = 1800

    def cleanup(self):
        """
        Deletes the build nodes and clients out of the Chef server.
        """

        for row in chef.Search('node', 'name:*.internal'):
            node = chef.Node(row.object.name)
            chef.Node.delete(node)

    def stopServers(self, instance_ids):
        """
        Stops instances specified in list. It will also stop any servers listed in failed_ids as a cleanup measure.
        :param instances_ids:
        List of instance ids to stop
        """

        status = 0
        stopped = []

        print "Stopping instances..."

        totalInstances = len(instance_ids)
        stoppedCount = 0

        for instance_id in instance_ids:
            try:
                self.ec2.stop_instances(instance_id.encode('ascii'))
            except Exception as e:
                print "Failed to issue stop command for {0}".format(instance_id.encode('ascii'))
                print e.message
        try:
            reservations = self.ec2.get_all_reservations(filters={'reservation_id':failed_ids})
        except Exception as e:
            print "Failed to get instance reservations."
            print e.message

        instances = [i for r in reservations for i in r.instances]

        for instance in instances:
            try:
                self.ec2.stop_instances(instance.id)
            except Exception as e:
                print "Failed to issue stop command for {0}".format(instance_id.encode('ascii'))
                print e.message

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

    def buildList(self):
        """
        Builds a list of cluster data for autoscaling clusters.
        We query the Chef server for nodes with autoscaling enabled and add their properties to the list to be
        returned.
        :return:
        """

        cluster_data = []

        print "Using AMI ID: {0}".format(imageId)
        print "Finding clusters with autoscaling enabled..."

        for name, item in self.bag.iteritems():
            for row in chef.Search('node', 'cluster:' + name + " AND chef_environment:prod"):
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

                        cluster_data.append((name, "prod", roles, node['ec2']['security_groups']))
                        break

        return cluster_data

    def buildServers(self, cluster_data):
        """
        Bootstraps and builds autoscaling servers to be used for AMI imaging.
        :param cluster_data:
        List of the following format: [(name,env,roles,securityGroups),]
        """

        reservation_ids = []
        instance_ids = []
        status = 0
        now = time.time()
        timelimit = now + self.threshold

        print "Building servers via Chef to be used for imaging..."
        for cluster, env, role, securityGroups in cluster_data:
            time.sleep(1)
            userData = 'HOSTNAME=chef-autobuild01 ENV=prod CLUSTER=' + cluster + ' AUTOSCALE=1 ROLES=' + role

            try:
                reservation = self.ec2.run_instances(image_id=imageId, key_name='ffe-ec2', security_groups=securityGroups,
                                            instance_type='c1.xlarge', user_data=userData)
                print "Launched " + reservation.id
                reservation_ids.append((reservation.id))
            except Exception as e:
                print "Failed to launch instance for cluster: {0}".format(cluster)
                print e.message

        print "Waiting for instances to complete the build process..."
        while status == 0:
            time.sleep(10)

            if time.time() >= timelimit:
                for r_id in reservation_ids:
                    reservation_ids.remove(r_id)
                    failed_ids.append(r_id)

            for r_id in reservation_ids:
                for row in chef.Search('node', 'ec2_reservation_id:' + r_id + " AND chef_environment:prod", 1):
                    node = chef.Node(row.object.name)
                    if node is not None and len(row) > 0:
                        sys.stdout.write("\n Cluster: " + node['cluster'] + " Instance ID: " + node['ec2']['instance_id']
                                         + " registered with chef.\n")
                        sys.stdout.flush()
                        reservation_ids.remove(r_id)
                        instance_ids.append((node['ec2']['instance_id']))

            if not reservation_ids:
                status=1

        return instance_ids

    def createImages(self, stopped):
        """
        Creates AMI images for the specified instances.
        :param stopped:
        List of instances in the stopped state.
        """

        completed = []
        ami_ids = []
        status = 0
        now = time.time()
        timelimit = now + self.threshold

        print "Starting AMI imaging..."
        timestamp = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
        for instance_id in stopped:
            for row in chef.Search('node', 'ec2_instance_id:' + instance_id + " AND chef_environment:prod", 1):
                time.sleep(1)
                node = chef.Node(row.object.name)
                if node is not None and len(row) > 0:
                    try:
                        ami = self.ec2.create_image(instance_id.encode('ascii'),node['cluster'] + "-autoscale-" + timestamp)
                        ami_ids.append((ami, node['cluster']))
                    except Exception as e:
                        print "Failed to issue create_image for {0}".format(instance_id)
                        print e.message

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
    cluster_data = autoscale.buildList()
    instance_ids = autoscale.buildServers(cluster_data)
    stopped = autoscale.stopServers(instance_ids)
    completed = autoscale.createImages(stopped)
    autoscale.cleanup()

    print "Run complete."
    print "SUMMARY:"
    print "-------------------------------"
    print "     AMI     |   CLUSTER       "
    print "-------------------------------"
    for ami, cluster in completed:
        print ami, cluster

    print "-------------------------------"
    print "FAILED BUILDS/AMIS"
    print "-------------------------------"
    for failed_id in failed_ids:
        print failed_id

    print "-------------------------------"

if __name__ == "__main__":
    main()
