# aws-asg-eip-manager

A lifecycle-hook AWS Lambda that gives Auto Scaling Group instances a public ENI
and a stable, per-AZ Elastic IP, with zero-traffic-loss EIP migration across
instance refreshes. Packaged as an [AWS Serverless Application Repository (SAR)](https://aws.amazon.com/serverless/serverlessrepo/)
application so you can attach it to any ASG by referencing a nested
`AWS::Serverless::Application` instead of carrying the Lambda inline.

## What it does

The ASG fires `EC2_INSTANCE_LAUNCHING` / `EC2_INSTANCE_TERMINATING` lifecycle
hooks, EventBridge routes them to this Lambda, and the Lambda:

- **On launch** - creates a public ENI in the instance's-AZ public subnet,
  attaches it (default device index 1), optionally clears source/dest check on
  both the new ENI and the instance's primary ENI (required for NAT), and
  associates an Elastic IP.
- **On terminate** - migrates the per-AZ pool EIP to a healthy same-AZ
  successor (the `EipsPerAz=1` path), optionally deregisters the instance from a
  GWLB/ELB target group first, releases auto-allocated EIPs, and tears the ENI
  down.

It always calls `CompleteLifecycleAction`, so a failure never wedges the ASG.

## The `EipsPerAz` pool model

`EipAllocationIds` is a flat list partitioned into `EipsPerAz`-sized pools in
`PublicSubnets` order (first `EipsPerAz` allocations belong to subnet 0, the next
`EipsPerAz` to subnet 1, and so on).

| `EipsPerAz` | Behavior |
|---|---|
| `0` | Pure ephemeral. Each instance auto-allocates a fresh EIP at launch, released at terminate. No stable egress IPs. |
| `1` | One stable IP per AZ. A refreshing instance temporarily uses a transient EIP; when the old instance terminates, its pool EIP migrates to the successor and the transient is released, so the AZ's stable IP returns. |
| `>=2` | Blue/green pool per AZ. A new instance claims a free pool slot; the old instance keeps its slot until terminate, then returns it. Egress IP is always one of the configured pool EIPs. |

## Using it

After the application is published to SAR (see below), reference it from your
own template. The ASG **lifecycle hooks stay in your template** (they belong to
the ASG resource); this app provides the Lambda and the EventBridge rule that
service them.

```yaml
Transform: AWS::Serverless-2016-10-31

Resources:
  EipManager:
    Type: AWS::Serverless::Application
    Properties:
      Location:
        ApplicationId: arn:aws:serverlessrepo:us-east-1:<ACCOUNT_ID>:applications/asg-eip-manager
        SemanticVersion: 0.1.0
      Parameters:
        PublicSubnets: !Join [',', !Ref PublicSubnets]
        PublicSecurityGroup: !Ref PublicEniSecurityGroup
        AsgName: !Sub '${AWS::StackName}-asg'
        EipAllocationIds: !Ref EipAllocationIds   # comma-separated, or '' for ephemeral
        EipsPerAz: 1
        # Optional - deregister from a GWLB target group before EIP migration:
        TargetGroupArn: !Ref GWLBTargetGroup
        TargetPort: 6081

  Asg:
    Type: AWS::AutoScaling::AutoScalingGroup
    Properties:
      AutoScalingGroupName: !Sub '${AWS::StackName}-asg'
      # ... launch template, subnets, etc ...
      LifecycleHookSpecificationList:
        - LifecycleHookName: !Sub '${AWS::StackName}-launch-hook'
          LifecycleTransition: autoscaling:EC2_INSTANCE_LAUNCHING
          HeartbeatTimeout: 300
          DefaultResult: ABANDON
        - LifecycleHookName: !Sub '${AWS::StackName}-terminate-hook'
          LifecycleTransition: autoscaling:EC2_INSTANCE_TERMINATING
          HeartbeatTimeout: 300
          DefaultResult: CONTINUE
```

> Replace `<ACCOUNT_ID>` and `SemanticVersion` with the values from your publish.

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `PublicSubnets` | (required) | Public subnets (one per AZ) the public ENI is created in. Order defines EIP-pool partitioning. |
| `PublicSecurityGroup` | (required) | Security group applied to the public ENI. |
| `AsgName` | (required) | Name of the ASG whose lifecycle actions this Lambda services. |
| `EipAllocationIds` | `''` | Comma-separated EIP allocation IDs, partitioned into `EipsPerAz`-sized pools. Empty for pure ephemeral. |
| `EipsPerAz` | `0` | EIP pool size per AZ (see the pool model above). |
| `EniDeviceIndex` | `1` | Device index the public ENI is attached at. |
| `DisableSrcDestCheck` | `true` | Disable source/dest check on the public ENI and the instance's primary ENI (required for NAT). |
| `TagNamespace` | `metaphor` | Namespace prefix for the tags the Lambda writes (e.g. `<ns>:managed`). |
| `TargetGroupArn` | `''` | Optional GWLB/ELB target group ARN; the terminating instance is deregistered from it before EIP migration. |
| `TargetPort` | `6081` | Target port used when deregistering from `TargetGroupArn`. |

## IAM model

The bundled Lambda role is least-privilege but the EC2 ENI/EIP actions
(`CreateNetworkInterface`, `AllocateAddress`, `AssociateAddress`, ...) cannot be
resource-scoped at create time, so they are granted on `Resource: '*'`. The role
also gets `autoscaling:CompleteLifecycleAction` / `DescribeAutoScalingGroups`,
and - only when `TargetGroupArn` is set - `elasticloadbalancing:DeregisterTargets`
/ `DescribeTargetHealth`. Logging permissions are scoped to the function's own
log group.

## Development

```sh
pip install -r requirements-dev.txt
pytest -q
```

Tests run entirely against [moto](https://github.com/getmoto/moto); no AWS
account is required.

Template validation:

```sh
sam validate --lint
sam build
```

## Publishing to SAR

CI publishes on a published GitHub release (`.github/workflows/release.yml`). It
requires these repo settings:

- `vars.AWS_ROLE_ARN` - an OIDC-assumable role with SAR + S3 publish permissions.
- `vars.SAR_ARTIFACT_BUCKET` - an S3 bucket in `us-east-1` for `sam package` artifacts.
- `vars.SAR_APPLICATION_ID` - (set after the first publish) the created
  application ARN, so subsequent releases keep it publicly deployable via
  `serverlessrepo put-application-policy` granting `Deploy` to `*`.

## License

Amazon Software License - see [LICENSE](LICENSE).
