# Highly Available CRUD Application on AWS

A three-tier notes application on AWS, built and automated end to end with Ansible: an Application Load Balancer in front of three Flask servers, MariaDB with one primary and two replicas (writes to the primary, reads spread across the replicas), and a Prometheus / Alertmanager / Grafana monitoring stack.

!Infrastructure(<img width="1132" height="782" alt="architecture drawio" src="https://github.com/user-attachments/assets/1a43b265-c56e-4d7a-af9a-21991c63c5ff" />

)

## What it demonstrates

- **Load balancing:** an ALB spreads traffic over 3 app servers across 2 availability zones, with health checks on `/health`.
- **Database replication:** MariaDB primary with 2 asynchronous replicas. The app writes to the primary and reads from a random replica, with fallback to the other replica and then the primary.
- **Observability:** node and MariaDB metrics from all VMs, Grafana dashboards, and alert rules (instance down, MariaDB down, replication stopped or lagging, low disk, low memory).
- **Automation:** every layer is configured with Ansible playbooks, so the environment can be rebuilt from an inventory file.
- **Network hardening:** only ports 22 and 80 are open to the internet. Internal ports are limited to the security group, and the monitoring UIs to a single admin IP.

## Architecture

![Request and replication flow](docs/img/arch-request-flow.png)

| Layer | Details |
|---|---|
| Cloud | AWS, `ap-south-1` (Mumbai), one VPC `10.0.0.0/16`, two subnets in two AZs, internet gateway |
| Load balancer | Application Load Balancer (internet-facing), target group on port 5000, health check `/health` |
| App tier | 3 x `t3.small` (Amazon Linux 2023), Python Flask + PyMySQL, run as a systemd service |
| Database tier | 3 x `t3.small`, MariaDB: 1 primary (`server-id=1`, binlog on) + 2 read-only replicas |
| Monitoring | 1 x `t2.micro`: Prometheus, Alertmanager, Grafana. node_exporter on all 7 VMs, mysqld_exporter on the 3 DB VMs |
| Automation | Ansible: `mariadb.yml`, `app.yml`, `monitoring.yml` |

### The application

A notes app with a browser UI and a JSON API.

- **UI (`/`):** add, edit, delete and search notes. The header shows which app server answered and which database node the data was read from. A "Database cluster" panel shows each node's status and note count, so replication is visible in the browser.
- **API:** `GET/POST /notes`, `GET/PUT/DELETE /notes/<id>`. Responses carry `X-Served-By` and `X-Read-From` headers.
- **`/health`:** used by the ALB. It deliberately does not query the database, so a DB problem doesn't make the load balancer drop every app server.
- **Read-your-writes:** after a write, the page re-reads once from the primary, so you see your own change even if a replica is a few milliseconds behind.

![Application UI](docs/img/app-ui.png)

### Monitoring

![Monitoring architecture](docs/img/arch-monitoring.png)

## Repository layout

```
inventory.ini     # hosts, private IPs, shared variables (keep secrets in Ansible Vault)
mariadb.yml       # primary + replicas, replication user, app database and user
app.yml           # Python deps, app code, .env, systemd service, health check
monitoring.yml    # node_exporter, mysqld_exporter, Prometheus, Alertmanager, Grafana
app.py            # Flask application
docs/             # architecture.drawio and screenshots
```

## Deploy

Prerequisites: an AWS account and CLI, Ansible, an EC2 key pair, and the infrastructure from the architecture diagram (VPC, 2 subnets, internet gateway and routes, security group, 7 EC2 instances, ALB and target group).

1. Fill in `inventory.ini` with each host's public address (`ansible_host`) and `private_ip`.
2. Run the playbooks in order:

```bash
ansible-playbook -i inventory.ini mariadb.yml      # database + replication
ansible-playbook -i inventory.ini app.yml          # app servers
ansible-playbook -i inventory.ini monitoring.yml   # exporters + Prometheus/Alertmanager/Grafana
```

![Ansible run](docs/img/ansible-recap.png)

3. Open the ALB DNS name in a browser.
4. Reach the monitoring UIs from the admin IP: Grafana on `:3000`, Prometheus on `:9090`, Alertmanager on `:9093`. Import Grafana dashboards **1860** (Node Exporter Full) and **7362** (MySQL Overview).

## Verification

### Load balancer

All three app servers are registered and healthy in the target group:

![Target group health](docs/img/alb-target-health.png)

Refreshing the page shows the serving app server changing as the ALB rotates requests, and the data being read from different replicas:

![Served by app server A](docs/img/lb-served-by-1.png)
![Served by app server B](docs/img/lb-served-by-2.png)

### Replication

Rows written through the ALB appear on both replicas, the replica threads run with no lag, and replicas reject writes from the app user (`--read-only`):

![Replication status](docs/img/replication-status.png)

### Monitoring and alerting

All scrape targets are up (3 MariaDB, 7 node, plus Prometheus itself):

![Prometheus targets](docs/img/prometheus-targets.png)

Dashboards under load (a script sends parallel read and write traffic through the ALB while CPU is burned on one app server):

![Node Exporter dashboard under load](docs/img/grafana-node-exporter.png)
![MySQL dashboard under load](docs/img/grafana-mysql-overview.png)

Stopping `node_exporter` on an app server fires `InstanceDown` after one minute:

![Alert firing](docs/img/alert-firing.png)

## Security

| Port | Source | Purpose |
|---|---|---|
| 22 | internet | SSH |
| 80 | internet | HTTP to the ALB |
| 5000 | security group only | ALB to the Flask app |
| 3306 | security group only | MariaDB client and replication traffic |
| 112 | security group only | reserved for VRRP (planned Keepalived) |
| 9100, 9104 | security group only | exporters, scraped by Prometheus |
| 9090, 3000, 9093 | admin IP only | Prometheus, Grafana, Alertmanager UIs |

Secrets such as the replication and application passwords should live in Ansible Vault. Use placeholders in anything you publish.

## Problems solved along the way

- **Azure to AWS.** The first attempt on Azure was blocked by capacity restrictions on the B-series VM sizes in a new subscription, across two regions. I moved the project to AWS rather than wait for a quota change.
- **Replication missed early changes.** A first playbook run failed partway (a regex bug) after it had already created the database and app user on the primary. Those events came before the binlog position captured on the successful retry, so the replicas never received them. I created the database and user on the replicas by hand, then restarted the replica threads.
- **ALB timed out with no error.** A timeout rather than a refused connection pointed to dropped packets. Checking the ALB's scheme, subnets, route tables and security groups in turn showed that the port 80 rule was missing from the security group.
- **ALB needs two subnets.** The load balancer requires two availability zones, so a second subnet was added.
- **Instance type not available in every AZ.** `t2.micro` is not offered in `ap-south-1c`, so the monitoring VM went into the second subnet in `ap-south-1b`.
- **Exporter user and replication.** The monitoring database user is created once on the primary and reaches the replicas through replication. The playbook waits until it exists on each replica before starting the exporter.

## Limitations and next steps

- **Database failover is manual.** Replicas are not promoted automatically. The next step is Keepalived with a floating IP: a notify script would move a secondary private IP through the AWS API (VRRP multicast doesn't work in a VPC, so it would use unicast), promote the replica and repoint the other one. Async replication means the last few transactions could be lost on failover.
- **Flask development server.** Production use would put gunicorn behind the ALB.
- **HTTPS.** Add an ALB listener on 443 with an ACM certificate.
- **More alerts.** A blackbox exporter for `/health`, a CPU alert, and a real Alertmanager receiver (email or Slack).
- **Infrastructure as code.** The VPC and instances were provisioned with the AWS CLI. Terraform would make that part repeatable too.

## Cost and cleanup

Running 6 x `t3.small`, 1 x `t2.micro` and an ALB costs money while they exist, so tear everything down when you are finished: terminate the instances, delete the ALB and target group, then the security group, subnets, route table, internet gateway and VPC.
