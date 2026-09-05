# Option King AI — Dedicated AWS Gateway Automation

Goal: normal customers never manage Termux, Railway IPs, gateway tokens, or a phone VPN. Each LIVE customer gets a dedicated AWS execution worker and public Elastic IP. The app shows only the assigned static IP for broker registration and live-readiness status.

## Runtime flow

1. Customer creates an Option King AI account.
2. Customer connects Angel One or Upstox in the app.
3. Paper trading is tested first.
4. Option King backend requests a dedicated AWS gateway for that OKAI user.
5. AWS creates or reuses one EC2 worker tagged `OKAIUserId=<user_id>`.
6. AWS creates or reuses one Elastic IP tagged to the same user and attaches it to that instance.
7. Backend stores that IP as the user's expected gateway IP.
8. App shows the assigned IP in Step 4 so the customer can register it in the broker developer portal.
9. The worker runs the existing `local_gateway_agent` as a systemd service and heartbeats to Railway.
10. Live is enabled only when entitlement + broker + gateway heartbeat + static-IP match are all ready and the customer explicitly confirms LIVE.

## Railway environment required

- `AWS_REGION` (recommended `ap-south-1`)
- AWS credentials supplied using a least-privilege IAM identity or an equivalent secure AWS integration
- `OKAI_GATEWAY_AMI_ID`
- `OKAI_GATEWAY_SUBNET_ID`
- `OKAI_GATEWAY_SECURITY_GROUP_ID`
- optional `OKAI_GATEWAY_INSTANCE_TYPE` (default `t3.micro`)
- optional `OKAI_GATEWAY_KEY_NAME`
- optional `OKAI_BACKEND_URL`

## Safety / isolation

- Never reuse the same Elastic IP across customer accounts.
- Never send the gateway token to the mobile/web UI.
- Tag every instance and EIP with the OKAI user id.
- Broker credentials stay per user and must not be shared between workers.
- Keep new real-money entries disabled until server and worker readiness agree.
- Preserve explicit customer confirmation before enabling live execution.
- Deactivation should disarm live immediately; infrastructure termination can be delayed until there are no open positions.

## Cost control

Use the smallest supported instance class that is reliable for the gateway worker. A dedicated public IPv4/EIP and instance both have ongoing AWS cost. Stop/terminate workers for customers whose paid/trial live access has ended only after confirming there are no open live positions.
