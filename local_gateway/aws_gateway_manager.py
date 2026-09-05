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
WORKER_PROFILE = os.getenv("OKAI_GATEWAY_WORKER_PROFILE", "OptionKingGatewayWorkerProfile")
REPO = os.getenv("OKAI_GATEWAY_REPO_URL", "https://github.com/ayush2726-maker/option-king-saas.git")
PROVISIONER_TOKEN = str(os.getenv("OKAI_PROVISIONER_TOKEN") or "").strip()
WORKER_REVISION = "cloud-gateway-v2"

EC2 = boto3.client("ec2", region_name=REGION)


def _worker_user_data(user_id, gateway_token):
    return f"""#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git ca-certificates
if command -v snap >/dev/null 2>&1; then
  snap install amazon-ssm-agent --classic || true
  systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service || true
fi
cd /opt
rm -rf option-king-saas
git clone --depth 1 {REPO} option-king-saas
cd option-king-saas/local_gateway_agent
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p /etc/okai
cat >/etc/okai/gateway.env <<'EOF'
OKAI_API_BASE={BACKEND}
OKAI_GATEWAY_TOKEN={gateway_token}
OKAI_USER_ID={int(user_id)}
PYTHONUNBUFFERED=1
EOF
chmod 600 /etc/okai/gateway.env
cat >/etc/systemd/system/okai-gateway.service <<'EOF'
[Unit]
Description=Option King AI Dedicated Cloud Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/option-king-saas/local_gateway_agent
EnvironmentFile=/etc/okai/gateway.env
ExecStart=/opt/option-king-saas/local_gateway_agent/.venv/bin/python -u /opt/option-king-saas/local_gateway_agent/okai_cloud_gateway.py
Restart=always
RestartSec=5
User=root

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
    found = EC2.describe_addresses(
        Filters=[
            {"Name": "tag:OKAIUserId", "Values": [str(int(user_id))]},
            {"Name": "tag:OKAIRole", "Values": ["worker"]},
        ]
    ).get("Addresses", [])
    if found:
        return found[0]
    address = EC2.allocate_address(Domain="vpc")
    EC2.create_tags(
        Resources=[address["AllocationId"]],
        Tags=[
            {"Key": "Name", "Value": f"okai-eip-u{int(user_id)}"},
            {"Key": "OKAIUserId", "Value": str(int(user_id))},
            {"Key": "OKAIRole", "Value": "worker"},
            {"Key": "ManagedBy", "Value": "OptionKingAI"},
        ],
    )
    return address


def _headers():
    return {"X-Provisioner-Token": PROVISIONER_TOKEN} if PROVISIONER_TOKEN else {}


def _post(path, payload):
    response = requests.post(BACKEND + path, json=payload, headers=_headers(), timeout=30)
    try:
        data = response.json()
    except Exception:
        data = {}
    if not response.ok:
        raise RuntimeError(data.get("detail") or response.text[:240] or f"HTTP {response.status_code}")
    return data


def _lease():
    response = requests.get(BACKEND + "/local-gateway/provision/lease", headers=_headers(), timeout=30)
    try:
        data = response.json()
    except Exception:
        data = {}
    if not response.ok:
        raise RuntimeError(data.get("detail") or response.text[:240] or f"HTTP {response.status_code}")
    return data.get("job")


def _wait_running(instance_id, timeout=150):
    deadline = time.time() + timeout
    last = "unknown"
    while time.time() < deadline:
        result = EC2.describe_instances(InstanceIds=[instance_id])
        reservations = result.get("Reservations", [])
        if reservations and reservations[0].get("Instances"):
            instance = reservations[0]["Instances"][0]
            last = str((instance.get("State") or {}).get("Name") or "unknown")
            if last == "running":
                return instance
            if last in {"terminated", "shutting-down"}:
                raise RuntimeError(f"Worker entered {last}")
        time.sleep(3)
    raise RuntimeError(f"Worker did not become running in time; state={last}")


def process(user_id):
    address = _address(user_id)
    static_ip = address["PublicIp"]
    worker = _existing_worker(user_id)

    if worker and worker.get("State", {}).get("Name") == "stopped":
        EC2.start_instances(InstanceIds=[worker["InstanceId"]])

    if worker:
        # Keep the customer's permanent EIP and existing token on harmless retries.
        instance_id = worker["InstanceId"]
        _wait_running(instance_id)
        EC2.associate_address(
            InstanceId=instance_id,
            AllocationId=address["AllocationId"],
            AllowReassociation=True,
        )
        _post(
            "/local-gateway/provision/ready",
            {"user_id": int(user_id), "state": "bootstrapping", "instance_id": instance_id, "static_ip": static_ip},
        )
        return

    allocated = _post(
        "/local-gateway/provision/allocate",
        {"user_id": int(user_id), "static_ip": static_ip},
    )
    gateway_token = str(allocated.get("gateway_token") or "").strip()
    if not gateway_token:
        raise RuntimeError("Gateway token was not issued for a new worker")

    run = EC2.run_instances(
        ImageId=AMI,
        InstanceType=INSTANCE_TYPE,
        MinCount=1,
        MaxCount=1,
        SubnetId=SUBNET,
        SecurityGroupIds=[SECURITY_GROUP],
        IamInstanceProfile={"Name": WORKER_PROFILE},
        UserData=_worker_user_data(user_id, gateway_token),
        MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
        BlockDeviceMappings=[{
            "DeviceName": "/dev/sda1",
            "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "DeleteOnTermination": True, "Encrypted": True},
        }],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"okai-gateway-u{int(user_id)}"},
                    {"Key": "OKAIUserId", "Value": str(int(user_id))},
                    {"Key": "OKAIRole", "Value": "worker"},
                    {"Key": "OKAIRevision", "Value": WORKER_REVISION},
                    {"Key": "ManagedBy", "Value": "OptionKingAI"},
                ],
            }
        ],
    )
    instance_id = run["Instances"][0]["InstanceId"]
    _wait_running(instance_id)
    EC2.associate_address(
        InstanceId=instance_id,
        AllocationId=address["AllocationId"],
        AllowReassociation=True,
    )
    _post(
        "/local-gateway/provision/ready",
        {"user_id": int(user_id), "state": "bootstrapping", "instance_id": instance_id, "static_ip": static_ip},
    )


def run_forever():
    print("OKAI AWS gateway manager started", flush=True)
    while True:
        job = None
        try:
            job = _lease()
            if job:
                process(int(job["user_id"]))
        except Exception as exc:
            print(f"PROVISIONING_WARNING {str(exc)[:400]}", flush=True)
            if job:
                try:
                    _post(
                        "/local-gateway/provision/ready",
                        {"user_id": int(job["user_id"]), "state": "error", "error": str(exc)[:400]},
                    )
                except Exception as inner:
                    print(f"PROVISIONING_ERROR_CALLBACK_WARNING {str(inner)[:220]}", flush=True)
        time.sleep(8)


if __name__ == "__main__":
    run_forever()
