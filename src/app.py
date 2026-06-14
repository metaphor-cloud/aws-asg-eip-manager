"""ASG EIP/ENI manager Lambda.

Attaches a public ENI + Elastic IP to each Auto Scaling Group instance on
launch and cleans up (or migrates) it on terminate, driven by ASG lifecycle
hooks delivered through EventBridge.

The egress-IP contract is governed by ``EIPS_PER_AZ``:

  0    pure ephemeral. Every instance auto-allocates a fresh EIP at launch,
       released at terminate. No stable egress IPs.
  1    1-slot pool per AZ. A new instance during a refresh allocates a
       transient EIP (the pool slot is still held by the outgoing instance).
       When the old instance terminates, ``migrate_pool_on_terminate`` moves
       the slot to the same-AZ successor and releases the transient, so the
       AZ's stable IP returns post-refresh.
  >=2  N-slot pool per AZ (blue/green). A new instance claims a currently-free
       pool slot; the old instance keeps its slot until terminate, then
       returns it to the pool. No transient EIPs. Egress IP is always one of
       the configured pool EIPs.

``EIP_ALLOCATION_IDS`` is a flat list partitioned by ``PUBLIC_SUBNETS`` order:
the first ``EIPS_PER_AZ`` allocations belong to subnet 0, the next
``EIPS_PER_AZ`` to subnet 1, and so on.

Configuration is entirely via environment variables so the same code can be
deployed against any ASG; defaults reproduce common NAT-appliance behavior.
"""

import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class Config:
    """Runtime configuration sourced from environment variables."""

    def __init__(self, env=None):
        env = env if env is not None else os.environ
        self.public_subnets = [s.strip() for s in env.get("PUBLIC_SUBNETS", "").split(",") if s.strip()]
        self.public_sg = env["PUBLIC_SECURITY_GROUP"]
        self.asg_name = env["ASG_NAME"]
        # Name/tag prefix for created resources; falls back to the ASG name.
        self.name_prefix = env.get("STACK_NAME") or self.asg_name
        self.eips_per_az = int(env.get("EIPS_PER_AZ", "0"))
        raw_eips = env.get("EIP_ALLOCATION_IDS", "")
        self.user_eips = [e.strip() for e in raw_eips.split(",") if e.strip()]
        self.device_index = int(env.get("ENI_DEVICE_INDEX", "1"))
        self.disable_src_dest = env.get("DISABLE_SRC_DEST_CHECK", "true").strip().lower() in ("1", "true", "yes")
        self.tag_namespace = env.get("TAG_NAMESPACE", "metaphor").strip() or "metaphor"
        self.target_group_arn = env.get("TARGET_GROUP_ARN", "").strip()
        self.target_port = int(env.get("TARGET_PORT", "6081"))

    def tag_key(self, suffix):
        return f"{self.tag_namespace}:{suffix}"


class Clients:
    """boto3 clients, grouped so tests can construct them inside a moto mock."""

    def __init__(self):
        self.ec2 = boto3.client("ec2")
        self.asg = boto3.client("autoscaling")
        self.elb = boto3.client("elbv2")


def build_az_map(cfg, clients):
    """Map each public subnet's AZ to its subnet id and EIP pool.

    Pools are ``EIPS_PER_AZ``-sized slices of ``EIP_ALLOCATION_IDS`` taken in
    ``PUBLIC_SUBNETS`` order. With ``EIPS_PER_AZ=0`` every pool is empty.
    """
    subs = clients.ec2.describe_subnets(SubnetIds=cfg.public_subnets)["Subnets"]
    subs_sorted = sorted(subs, key=lambda s: cfg.public_subnets.index(s["SubnetId"]))
    az_map = {}
    for i, s in enumerate(subs_sorted):
        start = i * cfg.eips_per_az
        pool = cfg.user_eips[start:start + cfg.eips_per_az] if cfg.eips_per_az > 0 else []
        az_map[s["AvailabilityZone"]] = {"subnet": s["SubnetId"], "pool": pool}
    return az_map


def make_tags(cfg, iid):
    return [
        {"Key": "Name", "Value": f"{cfg.name_prefix}-public-{iid}"},
        {"Key": cfg.tag_key("managed"), "Value": "true"},
        {"Key": cfg.tag_key("instance"), "Value": iid},
        {"Key": cfg.tag_key("stack-name"), "Value": cfg.name_prefix},
    ]


def create_and_attach_eni(cfg, clients, iid, subnet_id, tags):
    """Create a public ENI, attach it to the instance, and (optionally) clear
    source/dest check on both it and the instance's primary ENI."""
    eni = clients.ec2.create_network_interface(
        SubnetId=subnet_id,
        Groups=[cfg.public_sg],
        Description=f"{cfg.name_prefix} public ENI - {iid}",
        TagSpecifications=[{"ResourceType": "network-interface", "Tags": tags}],
    )
    ni = eni["NetworkInterface"]["NetworkInterfaceId"]
    if cfg.disable_src_dest:
        clients.ec2.modify_network_interface_attribute(NetworkInterfaceId=ni, SourceDestCheck={"Value": False})
    clients.ec2.attach_network_interface(NetworkInterfaceId=ni, InstanceId=iid, DeviceIndex=cfg.device_index)
    for _ in range(30):
        st = clients.ec2.describe_network_interfaces(NetworkInterfaceIds=[ni])["NetworkInterfaces"][0]
        if st.get("Attachment", {}).get("Status") == "attached":
            break
        time.sleep(1)
    if cfg.disable_src_dest:
        attached = clients.ec2.describe_network_interfaces(Filters=[
            {"Name": "attachment.instance-id", "Values": [iid]},
        ])["NetworkInterfaces"]
        for p in attached:
            if p.get("Attachment", {}).get("DeviceIndex") == 0:
                clients.ec2.modify_network_interface_attribute(
                    NetworkInterfaceId=p["NetworkInterfaceId"], SourceDestCheck={"Value": False})
    return ni


def find_free_pool_eip(clients, pool):
    """Return the AllocationId of the first unassociated EIP in the pool, else None."""
    if not pool:
        return None
    try:
        addrs = clients.ec2.describe_addresses(AllocationIds=pool)["Addresses"]
    except Exception as e:
        logger.error("describe_addresses for pool %s failed: %s", pool, e)
        return None
    for a in addrs:
        if not a.get("AssociationId"):
            return a["AllocationId"]
    return None


def allocate_transient(cfg, clients, iid, az):
    """Allocate a fresh EIP tagged so terminate() knows to release it."""
    tags = make_tags(cfg, iid) + [
        {"Key": cfg.tag_key("auto-allocated"), "Value": "true"},
        {"Key": cfg.tag_key("role"), "Value": "transient"},
        {"Key": cfg.tag_key("az-slot"), "Value": az},
    ]
    eip = clients.ec2.allocate_address(
        Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": tags}])
    logger.info("allocated transient EIP %s for %s in %s", eip["AllocationId"], iid, az)
    return eip["AllocationId"]


def launch(cfg, clients, iid):
    r = clients.ec2.describe_instances(InstanceIds=[iid])
    az = r["Reservations"][0]["Instances"][0]["Placement"]["AvailabilityZone"]
    az_map = build_az_map(cfg, clients)
    if az not in az_map:
        raise Exception(f"no public subnet in AZ {az} for instance {iid}")
    subnet_id = az_map[az]["subnet"]
    pool = az_map[az]["pool"]
    ni = create_and_attach_eni(cfg, clients, iid, subnet_id, make_tags(cfg, iid))
    try:
        alloc_id = find_free_pool_eip(clients, pool)
        if alloc_id:
            logger.info("claiming pool EIP %s for %s in %s", alloc_id, iid, az)
            clients.ec2.associate_address(AllocationId=alloc_id, NetworkInterfaceId=ni, AllowReassociation=True)
        else:
            if pool:
                logger.info("pool in %s exhausted (%d EIP(s) all associated); allocating transient for %s",
                            az, len(pool), iid)
            else:
                logger.info("no pool configured for %s (EipsPerAz=0); allocating per-instance EIP for %s", az, iid)
            alloc_id = allocate_transient(cfg, clients, iid, az)
            clients.ec2.associate_address(AllocationId=alloc_id, NetworkInterfaceId=ni)
        logger.info("launch complete for %s in %s with EIP %s", iid, az, alloc_id)
    except Exception as e:
        logger.error("launch failed for %s, deleting ENI %s: %s", iid, ni, e)
        try:
            clients.ec2.delete_network_interface(NetworkInterfaceId=ni)
        except Exception as cleanup_err:
            logger.error("ENI cleanup for %s failed: %s", iid, cleanup_err)
        raise


def find_successor(cfg, clients, terminating_iid, az):
    """Find another in-service Healthy instance in the same AZ within the ASG."""
    try:
        g = clients.asg.describe_auto_scaling_groups(
            AutoScalingGroupNames=[cfg.asg_name])["AutoScalingGroups"][0]
    except Exception as e:
        logger.error("describe_auto_scaling_groups(%s) failed: %s", cfg.asg_name, e)
        return None
    for i in g.get("Instances", []):
        if (i["InstanceId"] != terminating_iid and
                i.get("AvailabilityZone") == az and
                i.get("LifecycleState") == "InService" and
                i.get("HealthStatus") == "Healthy"):
            return i["InstanceId"]
    return None


def get_managed_eni(cfg, clients, iid):
    enis = clients.ec2.describe_network_interfaces(Filters=[
        {"Name": f"tag:{cfg.tag_key('managed')}", "Values": ["true"]},
        {"Name": f"tag:{cfg.tag_key('instance')}", "Values": [iid]},
    ])["NetworkInterfaces"]
    return enis[0] if enis else None


def migrate_pool_on_terminate(cfg, clients, iid):
    """EipsPerAz=1 only: move the pool EIP from this terminating instance to the
    same-AZ successor (which currently holds a transient EIP) so the AZ's stable
    IP returns post-refresh. For EipsPerAz=0 there is no pool to move; for
    EipsPerAz>=2 the successor already holds a different pool EIP and the
    terminating instance's pool EIP simply returns to the pool via the normal
    disassociate-but-don't-release path in terminate()."""
    if cfg.eips_per_az != 1:
        return
    try:
        inst = clients.ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    except Exception as e:
        logger.error("describe_instances(%s) failed: %s", iid, e)
        return
    az = inst["Placement"]["AvailabilityZone"]
    pool = build_az_map(cfg, clients).get(az, {}).get("pool", [])
    if not pool:
        return
    slot = pool[0]
    try:
        slot_addr = clients.ec2.describe_addresses(AllocationIds=[slot])["Addresses"][0]
    except Exception as e:
        logger.error("describe_addresses(%s) failed: %s", slot, e)
        return
    # Only migrate if the terminating instance currently holds the slot EIP.
    # Compare against its managed ENI rather than the address's InstanceId, which
    # is not populated for EIPs associated via a network interface.
    term_eni = get_managed_eni(cfg, clients, iid)
    if not term_eni or slot_addr.get("NetworkInterfaceId") != term_eni["NetworkInterfaceId"]:
        return
    successor_iid = find_successor(cfg, clients, iid, az)
    if not successor_iid:
        logger.warning("no same-AZ successor for %s; pool EIP %s will return to pool unowned", iid, slot)
        return
    succ_eni = get_managed_eni(cfg, clients, successor_iid)
    if not succ_eni:
        logger.warning("successor %s has no managed ENI; cannot migrate pool EIP %s", successor_iid, slot)
        return
    succ_eni_id = succ_eni["NetworkInterfaceId"]
    succ_alloc_before = succ_eni.get("Association", {}).get("AllocationId")
    if cfg.target_group_arn:
        try:
            clients.elb.deregister_targets(
                TargetGroupArn=cfg.target_group_arn, Targets=[{"Id": iid, "Port": cfg.target_port}])
            logger.info("deregistered %s from %s", iid, cfg.target_group_arn)
        except Exception as e:
            logger.error("deregister_targets for %s failed: %s", iid, e)
    try:
        clients.ec2.associate_address(AllocationId=slot, NetworkInterfaceId=succ_eni_id, AllowReassociation=True)
        logger.info("migrated pool EIP %s from %s to %s ENI %s", slot, iid, successor_iid, succ_eni_id)
    except Exception as e:
        logger.error("associate_address slot %s -> successor %s failed: %s", slot, successor_iid, e)
        return
    if succ_alloc_before and succ_alloc_before != slot:
        try:
            a = clients.ec2.describe_addresses(AllocationIds=[succ_alloc_before])["Addresses"][0]
            tag_dict = {t["Key"]: t["Value"] for t in a.get("Tags", [])}
            if tag_dict.get(cfg.tag_key("auto-allocated")) == "true":
                clients.ec2.release_address(AllocationId=succ_alloc_before)
                logger.info("released successor transient EIP %s", succ_alloc_before)
        except Exception as e:
            logger.error("releasing successor transient %s failed: %s", succ_alloc_before, e)


def terminate(cfg, clients, iid):
    try:
        migrate_pool_on_terminate(cfg, clients, iid)
    except Exception as e:
        logger.error("migrate_pool_on_terminate(%s) failed: %s", iid, e)
    enis = clients.ec2.describe_network_interfaces(Filters=[
        {"Name": f"tag:{cfg.tag_key('managed')}", "Values": ["true"]},
        {"Name": f"tag:{cfg.tag_key('instance')}", "Values": [iid]},
    ])
    for n in enis["NetworkInterfaces"]:
        ni = n["NetworkInterfaceId"]
        if n.get("Association"):
            aid = n["Association"].get("AssociationId")
            alid = n["Association"].get("AllocationId")
            # An EIP already migrated to a successor ENI (the EipsPerAz=1 path)
            # must not be disassociated here - only act on EIPs still on this ENI.
            on_this_eni = True
            if alid:
                try:
                    cur = clients.ec2.describe_addresses(AllocationIds=[alid])["Addresses"][0]
                    on_this_eni = cur.get("NetworkInterfaceId") == ni
                except Exception as e:
                    logger.error("could not inspect EIP %s: %s", alid, e)
            if aid and on_this_eni:
                try:
                    clients.ec2.disassociate_address(AssociationId=aid)
                except Exception as e:
                    logger.error("disassociate %s failed: %s", aid, e)
                if alid:
                    try:
                        addr = clients.ec2.describe_addresses(AllocationIds=[alid])["Addresses"][0]
                        tag_dict = {t["Key"]: t["Value"] for t in addr.get("Tags", [])}
                        if tag_dict.get(cfg.tag_key("auto-allocated")) == "true":
                            clients.ec2.release_address(AllocationId=alid)
                            logger.info("released auto-allocated EIP %s", alid)
                        else:
                            logger.info("returned pool EIP %s to pool (kept allocated)", alid)
                    except Exception as e:
                        logger.error("could not inspect/release EIP %s: %s", alid, e)
            elif alid and not on_this_eni:
                logger.info("EIP %s already migrated off ENI %s; leaving associated", alid, ni)
        if n.get("Attachment"):
            try:
                clients.ec2.detach_network_interface(AttachmentId=n["Attachment"]["AttachmentId"], Force=True)
                for _ in range(30):
                    s = clients.ec2.describe_network_interfaces(NetworkInterfaceIds=[ni])["NetworkInterfaces"][0]
                    if s["Status"] == "available":
                        break
                    time.sleep(1)
            except Exception as e:
                logger.error("detaching ENI %s for %s failed: %s", ni, iid, e)
        try:
            clients.ec2.delete_network_interface(NetworkInterfaceId=ni)
        except Exception as e:
            logger.error("deleting ENI %s for %s failed: %s", ni, iid, e)
        logger.info("cleaned up ENI %s for %s", ni, iid)


def handler(event, context):
    cfg = Config()
    clients = Clients()
    d = event["detail"]
    iid = d["EC2InstanceId"]
    hook = d["LifecycleHookName"]
    asg = d["AutoScalingGroupName"]
    try:
        if "LAUNCHING" in d["LifecycleTransition"]:
            launch(cfg, clients, iid)
        else:
            terminate(cfg, clients, iid)
    except Exception as e:
        logger.error("handler error for %s (%s): %s", iid, d["LifecycleTransition"], e)
    finally:
        clients.asg.complete_lifecycle_action(
            LifecycleHookName=hook, AutoScalingGroupName=asg,
            InstanceId=iid, LifecycleActionResult="CONTINUE")
