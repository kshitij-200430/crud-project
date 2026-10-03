#!/bin/bash
set -e

REGION="ap-south-1"
VPC_CIDR="10.0.0.0/16"
SUBNET_CIDR="10.0.0.0/24"
KEY_NAME="test-key"
INSTANCE_TYPE="t3.small"
AMI_ID=""   # filled in below via lookup

echo "=== Step 0: Look up latest Amazon Linux 2023 AMI for $REGION ==="
AMI_ID=$(aws ec2 describe-images \
  --region $REGION \
  --owners amazon \
  --filters "Name=name,Values=al2023-ami-2023.*-x86_64" \
            "Name=state,Values=available" \
  --query "sort_by(Images, &CreationDate)[-1].ImageId" \
  --output text)
echo "Using AMI: $AMI_ID"

echo "=== Step 1: VPC ==="
VPC_ID=$(aws ec2 create-vpc --region $REGION --cidr-block $VPC_CIDR \
  --query "Vpc.VpcId" --output text)
aws ec2 create-tags --region $REGION --resources $VPC_ID --tags Key=Name,Value=devops-resume-vpc

echo "=== Step 2: Internet Gateway ==="
IGW_ID=$(aws ec2 create-internet-gateway --region $REGION --query "InternetGateway.InternetGatewayId" --output text)
aws ec2 attach-internet-gateway --region $REGION --vpc-id $VPC_ID --internet-gateway-id $IGW_ID

echo "=== Step 3: Subnet ==="
SUBNET_ID=$(aws ec2 create-subnet --region $REGION --vpc-id $VPC_ID --cidr-block $SUBNET_CIDR \
  --query "Subnet.SubnetId" --output text)
aws ec2 modify-subnet-attribute --region $REGION --subnet-id $SUBNET_ID --map-public-ip-on-launch

echo "=== Step 4: Route table (public internet access) ==="
RT_ID=$(aws ec2 create-route-table --region $REGION --vpc-id $VPC_ID --query "RouteTable.RouteTableId" --output text)
aws ec2 create-route --region $REGION --route-table-id $RT_ID --destination-cidr-block 0.0.0.0/0 --gateway-id $IGW_ID
aws ec2 associate-route-table --region $REGION --subnet-id $SUBNET_ID --route-table-id $RT_ID

echo "=== Step 5: Security group ==="
SG_ID=$(aws ec2 create-security-group --region $REGION --group-name devops-resume-sg \
  --description "SG for devops resume project" --vpc-id $VPC_ID --query "GroupId" --output text)

aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG_ID --protocol tcp --port 22 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG_ID --protocol tcp --port 5000 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG_ID --protocol tcp --port 3306 --source-group $SG_ID
aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG_ID --protocol vrrp --source-group $SG_ID
aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG_ID --protocol tcp --port 9090 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG_ID --protocol tcp --port 3000 --cidr 0.0.0.0/0

echo "=== Step 6: Using existing key pair '$KEY_NAME' (make sure test-key.pem is available locally for SSH) ==="

echo "=== Step 7: Launch 3 app instances ==="
APP_IDS=$(aws ec2 run-instances --region $REGION \
  --image-id $AMI_ID \
  --instance-type $INSTANCE_TYPE \
  --key-name $KEY_NAME \
  --security-group-ids $SG_ID \
  --subnet-id $SUBNET_ID \
  --count 3 \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=app-vm}]" \
  --query "Instances[].InstanceId" --output text)
echo "App instance IDs: $APP_IDS"

echo "=== Step 8: Launch 3 DB instances ==="
DB_IDS=$(aws ec2 run-instances --region $REGION \
  --image-id $AMI_ID \
  --instance-type $INSTANCE_TYPE \
  --key-name $KEY_NAME \
  --security-group-ids $SG_ID \
  --subnet-id $SUBNET_ID \
  --count 3 \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=db-vm}]" \
  --query "Instances[].InstanceId" --output text)
echo "DB instance IDs: $DB_IDS"

echo "=== Waiting for instances to be running... ==="
aws ec2 wait instance-running --region $REGION --instance-ids $APP_IDS $DB_IDS

echo "=== Done. Instance details: ==="
aws ec2 describe-instances --region $REGION --instance-ids $APP_IDS $DB_IDS \
  --query "Reservations[].Instances[].{Name:Tags[?Key=='Name']|[0].Value,ID:InstanceId,Public:PublicIpAddress,Private:PrivateIpAddress}" \
  --output table

echo ""
echo "VPC ID: $VPC_ID"
echo "Subnet ID: $SUBNET_ID"
echo "Security Group ID: $SG_ID"
echo "SSH example: ssh -i test-key.pem ec2-user@<public-ip>"
