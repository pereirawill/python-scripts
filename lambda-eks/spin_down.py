import boto3
import subprocess
import json
import sys
import os
import base64
import time
import tempfile
import kubernetes
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import botocore.exceptions
from auth import EKSAuth

# AWS credentials
CI_ROLE_ARN = os.environ.get('CI_ROLE_ARN')
K8S_CLUSTER_NAME = os.environ.get('K8S_CLUSTER_NAME')
K8S_CLUSTER_REGION = os.environ.get('K8S_CLUSTER_REGION')
SLEEP_DURATION = os.environ.get('SLEEP_DURATION')

# Ensure SLEEP_DURATION is set and convert it to an integer
if SLEEP_DURATION is not None:
    try:
        SLEEP_DURATION = int(SLEEP_DURATION)
    except ValueError:
        print("Error: SLEEP_DURATION is not a valid integer.")
        sys.exit(1)
else:
    print("Error: SLEEP_DURATION is not set.")
    sys.exit(1)

def authenticate_to_cluster(eks_auth):

    # Details from EKS
    eks_client = boto3.client('eks', region_name='eu-west-2')
    
    eks_details = eks_client.describe_cluster(name=eks_auth.cluster_name)['cluster']

    # Saving the CA cert to a temp file (working around the Kubernetes client limitations)
    fp = tempfile.NamedTemporaryFile(delete=False)
    ca_filename = fp.name
    cert_bs = base64.urlsafe_b64decode(eks_details['certificateAuthority']['data'].encode('utf-8'))
    fp.write(cert_bs)
    fp.close()

    # Token for the EKS cluster
    token = eks_auth.get_token()

    # Kubernetes client config
    conf = kubernetes.client.Configuration()
    conf.host = eks_details['endpoint']
    conf.api_key['authorization'] = token
    conf.api_key_prefix['authorization'] = 'Bearer'
    conf.ssl_ca_cert = ca_filename
    k8s_client = kubernetes.client.ApiClient(conf)
    
    return k8s_client

# Scale down nodes in Kubernetes using the Kubernetes Python module
def scale_down_karpenter_nodes(eks_client):
    try:
        api_instance = client.CustomObjectsApi(eks_client)
        
        # Patch NodePool to reduce CPU to 0
        nodepool_name = f"{K8S_CLUSTER_NAME}-nodepool"
        body_json = {"spec": {"limits": {"cpu": "0"}}}
        
        api_instance.patch_cluster_custom_object(
            group="karpenter.sh",
            version="v1beta1",
            name=nodepool_name,
            plural="nodepools",
            body=body_json
        )
        
        print(f"NodePool '{nodepool_name}' patched successfully and scaled to 0 CPU.")
    except ApiException as e:
        print(f"Error scaling down Karpenter nodes (HTTP Status Code: {e.status}): {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)

# Retrieve the last K8s node using Kubernetes Python module
def get_last_k8s_node(eks_client):
    try:
        api_instance = client.CoreV1Api(eks_client)

        # Get pods with label selector
        pods = api_instance.list_namespaced_pod(namespace="kyverno", label_selector="app.kubernetes.io/component=admission-controller")

        # Extract the node name of the first pod
        last_k8s_node = pods.items[0].spec.node_name
        print(f"Last K8s node: {last_k8s_node}")
        return last_k8s_node
    except Exception as e:
        print(f"Error retrieving last K8s node: {e}")
        sys.exit(1)

# Retrieve the last K8s node again but get the aws specific instance id
def get_last_instance_id(last_k8s_node):
    try:
        ec2_client = boto3.client('ec2', region_name=K8S_CLUSTER_REGION)

        # Get the instance ID based on the private DNS name (last_k8s_node)
        response = ec2_client.describe_instances(
            Filters=[
                {"Name": "private-dns-name", "Values": [last_k8s_node]}
            ]   
        )

        # Extract the instance ID
        last_instance_id = response["Reservations"][0]["Instances"][0]["InstanceId"]
        print(f"Last EC2 instance ID: {last_instance_id}")
        return last_instance_id
    except botocore.exceptions.ClientError as e:
        print(f"Error retrieving last EC2 instance ID: {e}")
        sys.exit(1)

def cordon_node(eks_client, last_k8s_node):
    try:
        api_instance = client.CoreV1Api(eks_client)
        
        # Define the patch payload to mark the node as unschedulable
        patch = [{"op": "replace", "path": "/spec/unschedulable", "value": True}]

        # Perform the patch operation
        api_instance.patch_node(name=last_k8s_node, body=patch)
        print(f"Node with name {last_k8s_node} has been cordoned")
    except ApiException as e:
        print(f"Error when patching and cordoning node: {e.reason}")
        raise

# delete all pods apart from kyverno and karpenter
def delete_pods(eks_client, last_k8s_node):
    try:
        api_instance = client.CoreV1Api(eks_client)
        
       # List all pods on the cordoned node
        pods = api_instance.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={last_k8s_node}",
            watch=False
        )

        # Exclude Kyverno and Karpenter namespace from pod deletion
        excluded_namespaces = ["kyverno", "karpenter"]

        # Delete pods in namespaces other than the excluded namespace
        for pod in pods.items:
            pod_namespace = pod.metadata.namespace
            pod_name = pod.metadata.name
            
            if pod_namespace not in excluded_namespaces:
                print(f"Deleting pod {pod_name} in namespace {pod_namespace}")
                api_instance.delete_namespaced_pod(name=pod_name, namespace=pod_namespace, grace_period_seconds=0)
    except ApiException as e:
        print(f"Error when listing or deleting pods: {e.reason}")
        sys.exit(1)

def terminate_ec2_instances(last_instance_id):
    try:
        ec2_client = boto3.client('ec2', region_name=K8S_CLUSTER_REGION)

        # Get running instances in the Karpenter node pool
        instance_ids = []
        response = ec2_client.describe_instances(
            Filters=[
                {"Name": "instance-state-name", "Values": ["running"]},
                {"Name": "tag:karpenter.sh/managed-by", "Values": [K8S_CLUSTER_NAME]},
                {"Name": "tag:karpenter.sh/nodepool", "Values": [f"{K8S_CLUSTER_NAME}-nodepool"]},
            ]
        )

        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                instance_ids.append(instance["InstanceId"])

        # Terminate instances excluding the last instance if there is more than one
        if len(instance_ids) > 1:
            instance_ids.remove(last_instance_id)
            ec2_client.terminate_instances(InstanceIds=instance_ids)
            print(f"Instances terminated successfully: {instance_ids}")
        else:
            print("Only one instance is running")
        
        print("Waiting for all nodes but the last node to gracefully terminate")
        time.sleep(SLEEP_DURATION)
            
        ec2_client.terminate_instances(InstanceIds=[last_instance_id])
        print(f"Last instance terminated successfully: {last_instance_id}")
    except botocore.exceptions.ClientError as e:
        print(f"Error terminating EC2 instances: {e}")
        sys.exit(1)

def main(event, context):
    eks_auth = EKSAuth(cluster_name=K8S_CLUSTER_NAME, region=K8S_CLUSTER_REGION)
    eks_client = authenticate_to_cluster(eks_auth)
    scale_down_karpenter_nodes(eks_client)
    last_k8s_node = get_last_k8s_node(eks_client)
    last_instance_id = get_last_instance_id(last_k8s_node)
    if last_k8s_node:
        cordon_node(eks_client, last_k8s_node)
        delete_pods(eks_client, last_k8s_node)
    if last_instance_id:
        terminate_ec2_instances(last_instance_id)

if __name__ == "__main__":
    main()
