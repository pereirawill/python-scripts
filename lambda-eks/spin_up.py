import boto3
import kubernetes
import tempfile
import base64
import random
import json
import os
import sys
import time
import yaml
from botocore.exceptions import NoCredentialsError
from botocore.signers import RequestSigner
from kubernetes import client, config
from auth import EKSAuth

# AWS credentials and global vars
ENV_TYPE = os.environ.get('ENV_TYPE')
K8S_CLUSTER_NAME = os.environ.get('K8S_CLUSTER_NAME')
K8S_CLUSTER_REGION = os.environ.get('K8S_CLUSTER_REGION')

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

def get_cpu_value(env_type):
    
    # Construct the YAML file path
    yaml_file = "./values.yaml"

    # Load YAML content
    with open(yaml_file, 'r') as file:
        yaml_content = yaml.safe_load(file)

    # Extract CPU value
    cpu_value = yaml_content.get('NodePool', {}).get('limits', {}).get('cpu')
    return cpu_value

def scale_up_karpenter_nodes(eks_client, cpu_value):
    try:
        api_instance = kubernetes.client.CustomObjectsApi(eks_client)

        # Patch NodePool to change CPU to value in values.yaml
        nodepool_name = f"{K8S_CLUSTER_NAME}-nodepool"
        body_json = {"spec": {"limits": {"cpu": cpu_value}}}

        api_instance.patch_cluster_custom_object(
            group="karpenter.sh",
            version="v1beta1",
            name=nodepool_name,
            plural="nodepools",
            body=body_json
        )
        
        print(f"NodePool '{nodepool_name}' patched successfully and scaled up to {cpu_value} CPU.")
    except Exception as e:
        print(f"Error scaling up Karpenter nodes: {e}")
        sys.exit(1)

def main(event, context):
    eks_auth = EKSAuth(cluster_name=K8S_CLUSTER_NAME, region=K8S_CLUSTER_REGION)
    eks_client = authenticate_to_cluster(eks_auth)
    cpu_value = get_cpu_value(env_type=ENV_TYPE)
    scale_up_karpenter_nodes(eks_client, cpu_value)
    
if __name__ == "__main__":
    main()
