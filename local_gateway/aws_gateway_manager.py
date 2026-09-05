import os
import time

import boto3
import requests

BACKEND = os.getenv("OKAI_BACKEND_URL", "https://option-king-saas-production.up.railway.app").rstrip("/")
REGION = os.getenv("AWS_REGION", "ap-south-1")
AMI = os.getenv("OKAI_GATEWAY_AMI_ID", "ami-0c0fd09cfe77b59dc")
SUBNET = os.getenv("OKAI_GATEWAY_SUBNET_ID", "subnet-0433b4048f14cd4c4")
SECURITY_GROUP = os.getenv("OKAI_GATEWAY_SECURITY_GROUP_ID", "sg-02caaa734cccbb139")
INSTANCE_TYPE = os.getenv("OKAI_GATEWAY_INSTANCE_TYPE", "t3.micro")
REPO = os.getenv("OKAI_GATEWAY_REPO_URL", "https://github.com/ayush2726-maker/option-king-saas.git")

EC2 = boto3.client("ec2", region_name=REGION)


def _worker_user_data(user_id, gateway_token):
    return f"""#!/bin/bash
set -e
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git ca-certificates
cd /opt
rm -rf option-king-saas
git clone {REPO} option-king-saas
cd option-king-saas/local_gateway_agent
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p /etc/okai
printf 'OKAI_API_BASE=%s\\nOKAI_GATEWAY_TOKEN=%s\\nOKAI_USER_ID=%s\\n' '{BACKEND}' '{gateway_token}' '{int(user_id)}' >/etc/okai/gateway.env
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
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable okai-gateway.service
systemctl restart okai-gateway.service
"""


def _existing_worker(user_id):
    result = EC2.describe_instances(
        Filters=[
            {"Name": "tag:OKAIUserId", "Values": [str(int(user_id))]},
            {"Name": "tag:OKAIRole", "Values": ["worker"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ]
    )
    for reservation in result.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            return instance
    return None


def _address(user_id):
    found = EC2.describe_addresses(Filters=[{"Name": "tag:OKAIUserId", "Values": [str(int(user_id))]}]).get("Addresses", [])
    if found:
        return found[0]
    address = EC2.allocate_address(Domain="vpc")
    EC2.create_tags(
        Resources=[address["AllocationId"]],
        Tags=[
            {"Key": "Name", "Value": f"okai-eip-u{int(user_id)}"},
            {"Key": "OKAIUserId", "Value": str(int(user_id))},
            {"Key": "ManagedBy", "Value": "OptionKingAI"},
        ],
    )
    return address


def _post(path, payload):
    response = requests.post(BACKEND + path, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def process(user_id):
    address = _address(user_id)
    static_ip = address["PublicIp"]
    allocated = _post(
        "/local-gateway/provision/allocate",
        {"user_id": int(user_id), "static_ip": static_ip},
    )
    gateway_token = allocated["gateway_token"]
    worker = _existing_worker(user_id)
    if worker and worker.get("State", {}).get("Name") == "stopped":
        EC2.start_instances(InstanceIds=[worker["InstanceId"]])
    if not worker:
        run = EC2.run_instances(
            ImageId=AMI,
            InstanceType=INSTANCE_TYPE,
            MinCount=1,
            MaxCount=1,
            SubnetId=SUBNET,
            SecurityGroupIds=[SECURITY_GROUP],
            UserData=_worker_user_data(user_id, gateway_token),
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": f"okai-gateway-u{int(user_id)}"},
                        {"Key": "OKAIUserId", "Value": str(int(user_id))},
                        {"Key": "OKAIRole", "Value": "worker"},
                        {"Key": "ManagedBy", "Value": "OptionKingAI"},
                    ],
                }
            ],
        )
        worker = run["Instances"][0]
    EC2.associate_address(
        InstanceId=worker["InstanceId"],
        AllocationId=address["AllocationId"],
        AllowReassociation=True,
    )
    _post(
        "/local-gateway/provision/ready",
        {"user_id": int(user_id), "state": "bootstrapping"},
    )


def run_forever():
    while True:
        job = None
        try:
            response = requests.get(BACKEND + "/local-gateway/provision/lease", timeout=30)
            if response.ok:
                job = (response.json() or {}).get("job")
                if job:
                    process(int(job["user_id"]))
        except Exception as exc:
            if job:
                try:
                    _post(
                        "/local-gateway/provision/ready",
                        {"user_id": int(job["user_id"]), "state": "error", "error": str(exc)[:400]},
                    )
                except Exception:
                    pass
        time.sleep(10)


if __name__ == "__main__":
    run_forever()
