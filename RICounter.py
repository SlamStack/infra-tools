#!/usr/bin/env python
"""
RICounter - outputs RI balance for the current AWS region

negative balances indicate excess reserved instances
positive balances indicate instances that are not falling under RIs
"""
__author__ = 'devon.bleak@fox.com'

from collections import Counter;
from os import getenv;
import boto.ec2;
import boto.rds;

ec2 = boto.ec2.connect_to_region(getenv('AWS_REGION'));

reservations = ec2.get_all_reservations();

instances = [i.placement + ' - ' + i.instance_type for r in reservations for i in r.instances if i.state == 'running'];

instance_counter = Counter(instances);

for ri in ec2.get_all_reserved_instances():
	if ri.state == 'active':
		instance_counter.subtract({ri.availability_zone + ' - ' + ri.instance_type: ri.instance_count});

for key in sorted(instance_counter.keys()):
	print "%s\t%d" % (key, instance_counter[key]);
