import base64
import os
import time
from typing import Dict, Optional

try:
    import boto3
except Exception:
    boto3 = None


REGION = os.getenv("AWS_REGION", "ap-south-1")
AMI_ID = os.getenv("OKAI_GATEWAY_AMI_ID", "")
INSTANCE_TYPE = os.getenv("OKAI_GATEWAY_INSTANCE_TYPE", "t3.micro")
SUBNET_ID = os.getenv("OKAI_GATEWAY_SUBNET_ID", "")
SECURITY_GROUP_ID = os.getenv("OKAI_GATEWAY_SECURITY_GROUP_ID", "")
KEY_NAME = os.getenv("OKAI_GATEWAY_KEY_NAME", "")
BACKEND_URL = os.getenv("OKAI_BACKEND_URL", "https://option-king-saas-production.up.railway.app")
REPO_URL = os.getenv("OKAI_GATEWAY_REPO_URL", "https://github.com/ayush2726-maker/option-king-saas.git")


def configured() -> bool:
    return bool(boto3 and AMI_ID and SUBNET_ID and SECURITY_GROUP_ID)


def _ec2():
    if not boto3:
        raise RuntimeError("boto3 is not installed")
    return boto3.client("ec2", region_name=REGION)


def _user_data(gateway_token: str, user_id: int) -> str:
    # Gateway token is written only on the customer's dedicated worker.
    # Never expose it to the mobile/web UI.
    script = f'''#!/bin/bash
set -euo pipefail
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git ca-certificates
useradd -m -s /bin/bash okai || true
cd /opt
rm -rf option-king-saas
git clone {REPO_URL} option-king-saas
cd option-king-saas/local_gateway_agent
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p /etc/okai
cat >/etc/okai/gateway.env <<'EOF'
OKAI_API_BASE={BACKEND_URL}
OKAI_GATEWAY_TOKEN={gateway_token}
OKAI_USER_ID={int(user_id)}
EOF
chmod 600 /etc/okai/gateway.env
cat >/etc/systemd/system/okai-gateway.service <<'EOF'
[Unit]
Description=Option King AI Dedicated Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/option-king-saas/local_gateway_agent
EnvironmentFile=/etc/okai/gateway.env
ExecStart=/opt/option-king-saas/local_gateway_agent/.venv/bin/python okai_local_gateway_v2.py run
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable okai-gateway.service
systemctl restart okai-gateway.service
'''
    return script


def find_existing_instance(user_id: int) -> Optional[Dict]:
    if not configured():
        return None
    ec2 = _ec2()
    result = ec2.describe_instances(Filters=[
        {"Name": "tag:OKAIUserId", "Values": [str(int(user_id))]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
    ])
    for reservation in result.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            return instance
    return None


def ensure_dedicated_gateway(user_id: int, gateway_token: str) -> Dict:
    if not configured():
        return {
            "success": False,
            "state": "AWS_NOT_CONFIGURED",
            "detail": "AWS gateway automation is not configured on the server",
        }

    ec2 = _ec2()
    existing = find_existing_instance(user_id)
    instance_id = None
    if existing:
        instance_id = existing.get("InstanceId")
        state = (existing.get("State") or {}).get("Name")
        if state == "stopped":
            ec2.start_instances(InstanceIds=[instance_id])
    else:
        kwargs = {
            "ImageId": AMI_ID,
            "InstanceType": INSTANCE_TYPE,
            "MinCount": 1,
            "MaxCount": 1,
            "SubnetId": SUBNET_ID,
            "SecurityGroupIds": [SECURITY_GROUP_ID],
            "UserData": _user_data(gateway_token, user_id),
            "TagSpecifications": [{
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"okai-gateway-u{int(user_id)}"},
                    {"Key": "OKAIUserId", "Value": str(int(user_id))},
                    {"Key": "ManagedBy", "Value": "OptionKingAI"},
                ],
            }],
        }
        if KEY_NAME:
            kwargs["KeyName"] = KEY_NAME
        run = ec2.run_instances(**kwargs)
        instance_id = run["Instances"][0]["InstanceId"]

    # Reuse an existing EIP tagged to this user when possible; otherwise allocate one.
    addresses = ec2.describe_addresses(Filters=[
        {"Name": "tag:OKAIUserId", "Values": [str(int(user_id))]},
    ]).get("Addresses", [])

    if addresses:
        address = addresses[0]
        allocation_id = address.get("AllocationId")
        public_ip = address.get("PublicIp")
    else:
        address = ec2.allocate_address(Domain="vpc")
        allocation_id = address["AllocationId"]
        public_ip = address["PublicIp"]
        ec2.create_tags(Resources=[allocation_id], Tags=[
            {"Key": "Name", "Value": f"okai-eip-u{int(user_id)}"},
            {"Key": "OKAIUserId", "Value": str(int(user_id))},
            {"Key": "ManagedBy", "Value": "OptionKingAI"},
        ])

    ec2.associate_address(InstanceId=instance_id, AllocationId=allocation_id, AllowReassociation=True)

    return {
        "success": True,
        "state": "PROVISIONING",
        "instance_id": instance_id,
        "allocation_id": allocation_id,
        "static_ip": public_ip,
        "region": REGION,
    }


def terminate_dedicated_gateway(user_id: int) -> Dict:
    if not configured():
        return {"success": False, "state": "AWS_NOT_CONFIGURED"}
    ec2 = _ec2()
    instance = find_existing_instance(user_id)
    if instance:
        ec2.terminate_instances(InstanceIds=[instance["InstanceId"]])
    addresses = ec2.describe_addresses(Filters=[
        {"Name": "tag:OKAIUserId", "Values": [str(int(user_id))]},
    ]).get("Addresses", [])
    for address in addresses:
        assoc = address.get("AssociationId")
        if assoc:
            try:
                ec2.disassociate_address(AssociationId=assoc)
            except Exception:
                pass
        allocation_id = address.get("AllocationId")
        if allocation_id:
            try:
                ec2.release_address(AllocationId=allocation_id)
            except Exception:
                pass
    return {"success": True, "state": "TERMINATING"}
