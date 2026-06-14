"""Behavior tests for the ASG EIP/ENI manager, exercised against moto."""

from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import app

REGION = "us-east-1"
AZ_A = "us-east-1a"
AZ_B = "us-east-1b"


@pytest.fixture
def aws():
    with mock_aws():
        yield


def make_cfg(**overrides):
    env = {
        "PUBLIC_SECURITY_GROUP": overrides.pop("public_sg", "sg-placeholder"),
        "ASG_NAME": overrides.pop("asg_name", "test-asg"),
        "STACK_NAME": "test",
    }
    env.update({k.upper(): str(v) for k, v in overrides.items()})
    return app.Config(env=env)


def setup_network(ec2, azs):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    sg = ec2.create_security_group(GroupName="public", Description="public", VpcId=vpc)["GroupId"]
    subnets = []
    for i, az in enumerate(azs):
        sn = ec2.create_subnet(
            VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=az)["Subnet"]["SubnetId"]
        subnets.append(sn)
    return vpc, sg, subnets


def run_instance(ec2, subnet_id):
    return ec2.run_instances(
        ImageId="ami-12345678", MinCount=1, MaxCount=1, SubnetId=subnet_id,
    )["Instances"][0]["InstanceId"]


def alloc_eips(ec2, n):
    return [ec2.allocate_address(Domain="vpc")["AllocationId"] for _ in range(n)]


def addr(ec2, alloc_id):
    return ec2.describe_addresses(AllocationIds=[alloc_id])["Addresses"][0]


def all_alloc_ids(ec2):
    return {a["AllocationId"] for a in ec2.describe_addresses()["Addresses"]}


# --------------------------------------------------------------------------- #
# AZ-map partitioning
# --------------------------------------------------------------------------- #

def test_build_az_map_partitions_pool_by_subnet_order(aws):
    clients = app.Clients()
    _, sg, subnets = setup_network(clients.ec2, [AZ_A, AZ_B])
    eips = alloc_eips(clients.ec2, 4)
    cfg = make_cfg(
        public_sg=sg, public_subnets=",".join(subnets),
        eips_per_az=2, eip_allocation_ids=",".join(eips),
    )
    az_map = app.build_az_map(cfg, clients)
    assert az_map[AZ_A]["pool"] == eips[0:2]
    assert az_map[AZ_B]["pool"] == eips[2:4]
    assert az_map[AZ_A]["subnet"] == subnets[0]
    assert az_map[AZ_B]["subnet"] == subnets[1]


def test_build_az_map_empty_pool_when_eips_per_az_zero(aws):
    clients = app.Clients()
    _, sg, subnets = setup_network(clients.ec2, [AZ_A])
    cfg = make_cfg(public_sg=sg, public_subnets=subnets[0], eips_per_az=0)
    az_map = app.build_az_map(cfg, clients)
    assert az_map[AZ_A]["pool"] == []


# --------------------------------------------------------------------------- #
# launch
# --------------------------------------------------------------------------- #

def test_launch_claims_free_pool_eip(aws):
    clients = app.Clients()
    _, sg, subnets = setup_network(clients.ec2, [AZ_A])
    (eip,) = alloc_eips(clients.ec2, 1)
    cfg = make_cfg(public_sg=sg, public_subnets=subnets[0], eips_per_az=1, eip_allocation_ids=eip)
    iid = run_instance(clients.ec2, subnets[0])

    app.launch(cfg, clients, iid)

    eni = app.get_managed_eni(cfg, clients, iid)
    assert eni is not None
    assert addr(clients.ec2, eip)["NetworkInterfaceId"] == eni["NetworkInterfaceId"]
    # No transient allocated: still exactly the one pool EIP.
    assert all_alloc_ids(clients.ec2) == {eip}


def test_launch_allocates_transient_when_pool_exhausted(aws):
    clients = app.Clients()
    _, sg, subnets = setup_network(clients.ec2, [AZ_A])
    (eip,) = alloc_eips(clients.ec2, 1)
    cfg = make_cfg(public_sg=sg, public_subnets=subnets[0], eips_per_az=1, eip_allocation_ids=eip)
    iid1 = run_instance(clients.ec2, subnets[0])
    iid2 = run_instance(clients.ec2, subnets[0])

    app.launch(cfg, clients, iid1)   # claims the one pool EIP
    app.launch(cfg, clients, iid2)   # pool exhausted -> transient

    allocs = all_alloc_ids(clients.ec2)
    assert len(allocs) == 2
    transient = (allocs - {eip}).pop()
    eni2 = app.get_managed_eni(cfg, clients, iid2)
    assert addr(clients.ec2, transient)["NetworkInterfaceId"] == eni2["NetworkInterfaceId"]
    tags = {t["Key"]: t["Value"] for t in addr(clients.ec2, transient)["Tags"]}
    assert tags[cfg.tag_key("auto-allocated")] == "true"


def test_launch_allocates_transient_when_no_pool(aws):
    clients = app.Clients()
    _, sg, subnets = setup_network(clients.ec2, [AZ_A])
    cfg = make_cfg(public_sg=sg, public_subnets=subnets[0], eips_per_az=0)
    iid = run_instance(clients.ec2, subnets[0])

    app.launch(cfg, clients, iid)

    allocs = all_alloc_ids(clients.ec2)
    assert len(allocs) == 1
    eni = app.get_managed_eni(cfg, clients, iid)
    assert addr(clients.ec2, allocs.pop())["NetworkInterfaceId"] == eni["NetworkInterfaceId"]


def test_launch_deletes_eni_on_error(aws):
    clients = app.Clients()
    _, sg, subnets = setup_network(clients.ec2, [AZ_A])
    cfg = make_cfg(public_sg=sg, public_subnets=subnets[0], eips_per_az=0)
    iid = run_instance(clients.ec2, subnets[0])
    clients.ec2.associate_address = Mock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        app.launch(cfg, clients, iid)

    # ENI was cleaned up; nothing left tagged for this instance.
    assert app.get_managed_eni(cfg, clients, iid) is None


def test_launch_honors_device_index_and_tag_namespace(aws):
    clients = app.Clients()
    _, sg, subnets = setup_network(clients.ec2, [AZ_A])
    cfg = make_cfg(
        public_sg=sg, public_subnets=subnets[0], eips_per_az=0,
        eni_device_index=2, tag_namespace="acme",
    )
    iid = run_instance(clients.ec2, subnets[0])

    app.launch(cfg, clients, iid)

    enis = clients.ec2.describe_network_interfaces(Filters=[
        {"Name": "tag:acme:instance", "Values": [iid]},
    ])["NetworkInterfaces"]
    assert len(enis) == 1
    assert enis[0]["Attachment"]["DeviceIndex"] == 2


# --------------------------------------------------------------------------- #
# terminate
# --------------------------------------------------------------------------- #

def create_asg_with_instances(asg_name, subnet_id, count):
    asg = boto3.client("autoscaling", region_name=REGION)
    asg.create_launch_configuration(
        LaunchConfigurationName=f"{asg_name}-lc", ImageId="ami-12345678", InstanceType="t3.micro")
    asg.create_auto_scaling_group(
        AutoScalingGroupName=asg_name, LaunchConfigurationName=f"{asg_name}-lc",
        MinSize=count, MaxSize=count, DesiredCapacity=count, VPCZoneIdentifier=subnet_id)
    group = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])["AutoScalingGroups"][0]
    return [i["InstanceId"] for i in group["Instances"]]


def test_terminate_migrates_pool_eip_to_successor_and_releases_transient(aws):
    clients = app.Clients()
    _, sg, subnets = setup_network(clients.ec2, [AZ_A])
    (eip,) = alloc_eips(clients.ec2, 1)
    asg_name = "refresh-asg"
    iids = create_asg_with_instances(asg_name, subnets[0], 2)

    cfg = make_cfg(
        public_sg=sg, public_subnets=subnets[0], asg_name=asg_name,
        eips_per_az=1, eip_allocation_ids=eip,
    )

    old, new = iids[0], iids[1]
    app.launch(cfg, clients, old)   # claims pool EIP
    app.launch(cfg, clients, new)   # gets a transient
    new_transient = (all_alloc_ids(clients.ec2) - {eip}).pop()

    app.terminate(cfg, clients, old)

    # Pool EIP migrated onto the successor's managed ENI.
    new_eni = app.get_managed_eni(cfg, clients, new)
    assert addr(clients.ec2, eip)["NetworkInterfaceId"] == new_eni["NetworkInterfaceId"]
    # Successor's transient was released.
    with pytest.raises(ClientError):
        clients.ec2.describe_addresses(AllocationIds=[new_transient])
    # Old instance's managed ENI is gone.
    assert app.get_managed_eni(cfg, clients, old) is None


def test_terminate_deregisters_from_target_group(aws):
    clients = app.Clients()
    vpc, sg, subnets = setup_network(clients.ec2, [AZ_A])
    (eip,) = alloc_eips(clients.ec2, 1)
    asg_name = "dereg-asg"
    iids = create_asg_with_instances(asg_name, subnets[0], 2)
    tg = clients.elb.create_target_group(
        Name="tg", Protocol="HTTP", Port=80, VpcId=vpc, TargetType="instance",
    )["TargetGroups"][0]["TargetGroupArn"]
    old, new = iids[0], iids[1]
    clients.elb.register_targets(TargetGroupArn=tg, Targets=[{"Id": old, "Port": 80}])

    cfg = make_cfg(
        public_sg=sg, public_subnets=subnets[0], asg_name=asg_name,
        eips_per_az=1, eip_allocation_ids=eip, target_group_arn=tg, target_port=80,
    )
    app.launch(cfg, clients, old)
    app.launch(cfg, clients, new)

    app.terminate(cfg, clients, old)

    health = clients.elb.describe_target_health(TargetGroupArn=tg)["TargetHealthDescriptions"]
    assert all(h["Target"]["Id"] != old for h in health)


def test_terminate_releases_transient_eip(aws):
    clients = app.Clients()
    _, sg, subnets = setup_network(clients.ec2, [AZ_A])
    cfg = make_cfg(public_sg=sg, public_subnets=subnets[0], eips_per_az=0)
    iid = run_instance(clients.ec2, subnets[0])
    app.launch(cfg, clients, iid)
    transient = all_alloc_ids(clients.ec2).pop()

    app.terminate(cfg, clients, iid)

    with pytest.raises(ClientError):
        clients.ec2.describe_addresses(AllocationIds=[transient])
    assert app.get_managed_eni(cfg, clients, iid) is None


def test_terminate_keeps_pool_eip_allocated(aws):
    clients = app.Clients()
    _, sg, subnets = setup_network(clients.ec2, [AZ_A])
    eips = alloc_eips(clients.ec2, 2)
    cfg = make_cfg(
        public_sg=sg, public_subnets=subnets[0], eips_per_az=2,
        eip_allocation_ids=",".join(eips),
    )
    iid = run_instance(clients.ec2, subnets[0])
    app.launch(cfg, clients, iid)
    claimed = next(a for a in eips if addr(clients.ec2, a).get("AssociationId"))

    app.terminate(cfg, clients, iid)

    # Pool EIP still exists and is now free (disassociated, not released).
    a = addr(clients.ec2, claimed)
    assert a["AllocationId"] == claimed
    assert not a.get("AssociationId")
    assert app.get_managed_eni(cfg, clients, iid) is None


# --------------------------------------------------------------------------- #
# handler
# --------------------------------------------------------------------------- #

def test_handler_always_completes_lifecycle_action_on_error(monkeypatch):
    fake = SimpleNamespace(ec2=Mock(), asg=Mock(), elb=Mock())
    monkeypatch.setattr(app, "Clients", lambda: fake)
    monkeypatch.setattr(app, "Config", lambda: SimpleNamespace())
    monkeypatch.setattr(app, "launch", Mock(side_effect=RuntimeError("boom")))

    event = {"detail": {
        "EC2InstanceId": "i-abc",
        "LifecycleHookName": "launch-hook",
        "AutoScalingGroupName": "test-asg",
        "LifecycleTransition": "autoscaling:EC2_INSTANCE_LAUNCHING",
    }}
    app.handler(event, None)

    fake.asg.complete_lifecycle_action.assert_called_once_with(
        LifecycleHookName="launch-hook", AutoScalingGroupName="test-asg",
        InstanceId="i-abc", LifecycleActionResult="CONTINUE")


def test_handler_dispatches_terminate(monkeypatch):
    fake = SimpleNamespace(ec2=Mock(), asg=Mock(), elb=Mock())
    monkeypatch.setattr(app, "Clients", lambda: fake)
    monkeypatch.setattr(app, "Config", lambda: SimpleNamespace())
    term = Mock()
    monkeypatch.setattr(app, "terminate", term)
    monkeypatch.setattr(app, "launch", Mock(side_effect=AssertionError("should not launch")))

    event = {"detail": {
        "EC2InstanceId": "i-xyz",
        "LifecycleHookName": "terminate-hook",
        "AutoScalingGroupName": "test-asg",
        "LifecycleTransition": "autoscaling:EC2_INSTANCE_TERMINATING",
    }}
    app.handler(event, None)

    term.assert_called_once()
    assert term.call_args[0][2] == "i-xyz"
    fake.asg.complete_lifecycle_action.assert_called_once()
