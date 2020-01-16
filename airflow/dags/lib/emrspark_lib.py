import boto3
from botocore.exceptions import ClientError
import subprocess
import json
from pprint import pprint, pformat
import requests
import configparser
import time

import logging


# Setting UP
# ------------
def get_boto_clients(region_name, config=None):
    # If access and secret keys are empty, use the one stored by the OS.
    if config != None and config['AWS']['AWS_ACCESS_KEY_ID'] != '' and config['AWS']['AWS_SECRET_ACCESS_KEY'] != '':    
        ec2 = boto3.client('ec2', region_name=region_name,
                           aws_access_key_id=config['AWS']['AWS_ACCESS_KEY_ID'],
                           aws_secret_access_key=config['AWS']['AWS_SECRET_ACCESS_KEY']
                          )
        emr = boto3.client('emr', region_name=region_name,
                           aws_access_key_id=config['AWS']['AWS_ACCESS_KEY_ID'],
                           aws_secret_access_key=config['AWS']['AWS_SECRET_ACCESS_KEY']
                          )
        iam = boto3.client('iam', region_name=region_name,
                           aws_access_key_id=config['AWS']['AWS_ACCESS_KEY_ID'],
                           aws_secret_access_key=config['AWS']['AWS_SECRET_ACCESS_KEY']
                          )
    else:
        ec2 = boto3.client('ec2', region_name=region_name)
        emr = boto3.client('emr', region_name=region_name)
        iam = boto3.client('iam', region_name=region_name)
    return (ec2, emr, iam)


def get_first_available_vpc(ec2_client):
    return ec2_client.describe_vpcs().get('Vpcs', [{}])[0].get('VpcId', '')


def get_first_available_subnet(ec2_client, vpc_id):
    return ec2_client.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}, {'Name': 'state', 'Values': ['available']}])['Subnets'][0].get('SubnetId', '')
# ------------


# Create Security Group
# ------------
def create_security_group(ec2_client, name, desc, vpc_id, ip=None):
    """ Create a security group
    Args:
        - ec2_client (boto3.EC2.Client): EC2 client object.
        - name (string): Name of Security Group
        - desc (string): Description of Security Group
        - vpc_id (string): Name of VPC. If empty, use the first available VPC
        - ip (string): The IP address of this machine. Only this machine can connect to the cluster.
                       If empty, use https://api.ipify.org service to get public IP address.
    Return:
    
        dict: {
            'KeyFingerprint': 'string',
            'KeyMaterial': 'string',
            'KeyName': 'string',
            'KeyPairId': 'string'
        }
    """
    region = ec2_client.meta.region_name
    security_group_id = None
    
    try:
        # Do not create if we found an existing Security Group
        response = ec2_client.describe_security_groups(
            Filters=[
                {'Name':'group-name', 'Values': [name]}
            ]
        )
        groups = response['SecurityGroups']

        if ip is None:
            ip = requests.get('https://api.ipify.org').text

        if len(groups) > 0:
            # Update the rule to use the new IP address
            
            security_group_id = groups[0]['GroupId']
            logging.info('Found Security Group: %s in vpc %s (%s).' % (security_group_id, vpc_id, region))

            ip_permissions = groups[0]['IpPermissions']
            for ip_permission in ip_permissions:
                # Delete all rules that listens to TCP port 8998
                if ip_permission["IpProtocol"] == 'tcp' and ip_permission["FromPort"] == 8998 and ip_permission["FromPort"] == 8998:
                    cidr_ip = ip_permission['IpRanges'][0]['CidrIp']
                    revoke_status = ec2_client.revoke_security_group_ingress(
                        GroupId=security_group_id,
                        IpPermissions=[
                            {'IpProtocol': 'tcp',
                             'FromPort': 8998,
                             'ToPort': 8998,
                             'IpRanges': [{'CidrIp': cidr_ip}]
                            }
                        ])
            
            # Create a new inbound rule that listens to this machine's IP
            data = ec2_client.authorize_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[
                    {'IpProtocol': 'tcp',
                     'FromPort': 8998,
                     'ToPort': 8998,
                     'IpRanges': [{'CidrIp': '{}/32'.format(ip)}]}
                ])
            return groups[0]['GroupId']
        else:
            response = ec2_client.create_security_group(GroupName=name,
                                                 Description=desc,
                                                 VpcId=vpc_id)
            security_group_id = response['GroupId']
            logging.info('New Security Group created: %s in vpc %s (%s).' % (security_group_id, vpc_id, region))

            data = ec2_client.authorize_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[
                    {'IpProtocol': 'tcp',
                     'FromPort': 8998,
                     'ToPort': 8998,
                     'IpRanges': [{'CidrIp': '{}/32'.format(ip)}]}
                ])
            return security_group_id
    except ClientError as e:
        logging.error(e)
    return security_group_id
# ------------


# Recreate Default Roles and Key Pair
# ------------
def recreate_default_roles(iam_client):
    # Recreate default roles
    try:
        iam_client.remove_role_from_instance_profile(InstanceProfileName='EMR_EC2_DefaultRole', RoleName='EMR_EC2_DefaultRole')
        iam_client.delete_instance_profile(InstanceProfileName='EMR_EC2_DefaultRole')
        iam_client.detach_role_policy(RoleName='EMR_EC2_DefaultRole', PolicyArn='arn:aws:iam::aws:policy/service-role/AmazonElasticMapReduceforEC2Role')
        iam_client.delete_role(RoleName='EMR_EC2_DefaultRole')
        iam_client.detach_role_policy(RoleName='EMR_DefaultRole', PolicyArn='arn:aws:iam::aws:policy/service-role/AmazonElasticMapReduceRole')
        iam_client.delete_role(RoleName='EMR_DefaultRole')
    except iam_client.exceptions.NoSuchEntityException:
        pass
    logging.info("Output of recreate_default_roles:\n{}".format(
        json.loads(subprocess.check_output(['aws', 'emr', 'create-default-roles']))))


def recreate_key_pair(ec2_client, key_name):
    """
    Args:
        - ec2_client (boto3.EC2.Client): EC2 client object.
        - key_name (string): Name of key, usually 'xxx_pem'
    Return:
    
        dict: {
            'KeyFingerprint': 'string',
            'KeyMaterial': 'string',
            'KeyName': 'string',
            'KeyPairId': 'string'
        }
    """
    ec2_client.delete_key_pair(KeyName=key_name)
    keypair = ec2_client.create_key_pair(KeyName=key_name)
    logging.info("keypair {} created:\n{}".format(key_name, keypair))
    return keypair
# ------------


# Create EMR Cluster
# ------------
class ClusterError(Exception):
    def __init__(self, last_guess):
        self.last_guess = last_guess

        
def get_cluster_status(emr_client, cluster_id):
    cluster = emr_client.describe_cluster(ClusterId=cluster_id)
    return cluster['Cluster']['Status']['State']


def is_cluster_terminated(emr_client, cluster_id):
    cluster = emr_client.describe_cluster(ClusterId=cluster_id)
    return 'TERMINATED' in cluster['Cluster']['Status']['State']


def create_emr_cluster(emr_client, cluster_name, master_sg, slave_sg, keypair_name, subnet_id, job_flow_role='EMR_EC2_DefaultRole', service_role='EMR_DefaultRole', release_label='emr-5.9.0',
                   master_instance_type='m3.xlarge', num_core_nodes=3, core_node_instance_type='m3.xlarge'):
    """ Create an EMR cluster
    Args:
        - subnet_id (string): If empty, use first available VPC (VPC is inferred from Security Groups)
    """
    # Avoid recreating cluster
    clusters = emr_client.list_clusters(ClusterStates=['STARTING', 'RUNNING', 'WAITING', 'BOOTSTRAPPING'])
    active_clusters = [i for i in clusters['Clusters'] if i['Name'] == cluster_name]
    if len(active_clusters) > 0:
        return active_clusters[0]['Id']
    else:
        # Create cluster
        cluster_response = emr_client.run_job_flow(
            Name=cluster_name,
            ReleaseLabel=release_label,
            Instances={
                'InstanceGroups': [
                    {
                        'Name': "Master nodes",
                        'Market': 'ON_DEMAND',
                        'InstanceRole': 'MASTER',
                        'InstanceType': master_instance_type,
                        'InstanceCount': 1
                    },
                    {
                        'Name': "Slave nodes",
                        'Market': 'ON_DEMAND',
                        'InstanceRole': 'CORE',
                        'InstanceType': core_node_instance_type,
                        'InstanceCount': num_core_nodes
                    }
                ],
                'KeepJobFlowAliveWhenNoSteps': True,
                'Ec2SubnetId': subnet_id,
                'Ec2KeyName' : keypair_name,
                'EmrManagedMasterSecurityGroup': master_sg,
                'EmrManagedSlaveSecurityGroup': slave_sg
            },
            VisibleToAllUsers=True,
            JobFlowRole=job_flow_role,
            ServiceRole=service_role,
            Applications=[
                { 'Name': 'hadoop' },
                { 'Name': 'spark' },
                { 'Name': 'hive' },
                { 'Name': 'livy' },
                { 'Name': 'zeppelin' }
            ]
        )
        cluster_id = cluster_response['JobFlowId']
        cluster_state = get_cluster_status(emr_client, cluster_id)
        if cluster_state != 'STARTING':
            reason = emr_client.describe_cluster(ClusterId=cluster_id)['Cluster']['Status']['StateChangeReason']
            raise Exception("Cluster error: {} - {}".format(reason['Code'], reason['Message']))
            
        exit_loop = False
        while exit_loop == False:
            cluster_state = get_cluster_status(emr_client, cluster_id)
            if cluster_state == 'WAITING':
                exit_loop = True
            elif 'TERMINATED' in cluster_state:
                exit_loop = True
                raise Exception("Cluser terminated:\n{}".format(emr_client.describe_cluster(ClusterId=cluster_id)))
            else:
                logging.info("Cluster is {}. Waiting for completion...".format(cluster_state))
                time.sleep(10)
        logging.info("Cluser created:\n{}".format(emr_client.describe_cluster(ClusterId=cluster_id)))
        return cluster_id
# ------------


# Create Spark Session
# ------------
def is_cluster_ready(emr_client, cluster_id):
    return get_cluster_status(emr_client, cluster_id) == 'WAITING'


def get_cluster_dns(emr_client, cluster_id):
    cluster = emr_client.describe_cluster(ClusterId=cluster_id)
    return cluster['Cluster']['MasterPublicDnsName']


def spark_url(master_dns, location='', port=8998):
    """Get spark session url."""
    return 'http://{}:{}{}'.format(master_dns, port, location)


def kill_spark_session(master_dns, session_headers, port=8998):
    session_url = spark_url(master_dns, location=session_headers['Location'], port=port)
    requests.delete(session_url, headers={'Content-Type': 'application/json'})


def kill_spark_session_by_id(master_dns, session_id, port=8998):
    session_url = spark_url(master_dns, location='/sessions/{}'.format(session_id), port=port)
    requests.delete(session_url, headers={'Content-Type': 'application/json'})
    

def kill_all_inactive_spark_sessions(master_dns):
    response = requests.get(spark_url(master_dns, location='/sessions'))
    spark_sessions = response.json()['sessions']
    logging.info("Killing all inactive spark sessions")
    for spark_session in spark_sessions:
        if spark_session['state'] in ['idle', 'dead'] :
            kill_spark_session_by_id(master_dns, spark_session['id'])
            logging.info("Killed {} spark session id {}".format(spark_session['state'],
                                                                spark_session['id']))
    
    
def create_spark_session(master_dns, port=8998):
    session_url = spark_url(master_dns, location='/sessions', port=port)
    data = {'kind': 'pyspark', 
            "conf" : {"spark.jars.packages" : "saurfang:spark-sas7bdat:2.0.0-s_2.11",
                      "spark.driver.extraJavaOptions" : "-Dlog4jspark.root.logger=WARN,console"
                     }
           }
    headers = {'Content-Type': 'application/json'}
    response = requests.post(session_url, data=json.dumps(data), headers=headers)
    logging.info("Sent spark session creation command to {}".format(session_url))
    logging.info("Response headers: {}".format(response.headers))
    logging.info(response.json())
    if 'Location' not in response.headers:
        raise Exception("Spark session creation failed. This is usually due " + \
                        "to too many spark sessions on the server. " + \
                        "Please run kill_all_inactive_spark_sessions function.")
    return response.headers


def wait_for_spark(master_dns, session_headers, port=8998):
    """Wait until status is idle"""
    status = ''
    logging.info("Session headers: {}".format(session_headers))
    session_url = spark_url(master_dns, location=session_headers['Location'], port=port)
    while status not in ['idle', 'dead']:
        response = requests.get(session_url, headers=session_headers)
        status = response.json()['state']
        logging.info("Spark session status: {}".format(status))
        if status == 'dead':
            response_json = response.json()
            del(response_json['code'])
            raise Exception("Spark session is dead:\nResponse status code: {}\nHeaders: {}\nContent: {}" \
                            .format(response.status_code, response.headers, response_json))
        else:
            time.sleep(5)
# ------------


# Send Spark Jobs
# ------------
def push_args_into_code(code, args):
    # Include arguments into the code (at the top of the file)
    args_str = ""
    for key, value in args.items():
        if isinstance(value, str):
            args_str += "{}='{}'\n".format(key, value.replace("'", "\'"))
        else:
            args_str += "{}={}\n".format(key, value)
    code = args_str + code
    return code


def push_into_code(code, helpers):
    # Include helpers into the code (at the top of the file)
    code = helpers + "\n" + code
    return code

    
def submit_spark_job(master_dns, session_headers, code, args={}, port=8998):
    statements_url = spark_url(master_dns, location=session_headers['Location'] + "/statements", port=port)

    job = {'code': code}
    response = requests.post(statements_url, data=json.dumps(job),
                             headers={'Content-Type': 'application/json'})
    response_json = response.json()
    del(response_json['code'])
    if response.status_code not in [200, 201]:
        raise Exception("Spark job sending error:\nResponse status code: {}\nHeaders: {}\nContent: {}" \
                        .format(response.status_code, response.headers, pformat(response_json)))
    else:
        logging.info("Spark job sending successful:\nResponse status code: {}\nHeaders: {}\nContent: {}" \
                     .format(response.status_code, response.headers, pformat(response_json)))
    return response.headers
# ------------


# Send Spark Jobs from File
# ------------
def submit_spark_job_from_file(master_dns, session_headers, filepath, args={}, helperspath=None, commonpath=None, port=8998):
    with open(filepath, 'r') as f:
        code = f.read()
    helpers_code = ''
    if helperspath is not None:
        with open(helperspath, 'r') as f:
            helpers_code = f.read()
    common_code = ''
    if commonpath is not None:
        with open(commonpath, 'r') as f:
            common_code = f.read()

    code = push_into_code(code, helpers_code)
    code = push_into_code(code, common_code)
    code = push_args_into_code(code, args)

    return submit_spark_job(master_dns, session_headers, code, args=args, port=port)
# ------------


# Track Spark Job Status
# ------------
def track_spark_job(master_dns, job_response_headers, port=8998):
    job_status = ''
    session_url = spark_url(master_dns, location=job_response_headers['Location'].split('/statements', 1)[0], port=port)
    statement_url = spark_url(master_dns, location=job_response_headers['Location'], port=port)
        
    while job_status not in ['available']:
        # If a statement takes longer than a few milliseconds to execute, Livy returns early and provides
        # a statement URL that can be pooled until it is complete:
        statement_response = requests.get(statement_url, headers={'Content-Type': 'application/json'})
        response_json = statement_response.json()
        job_status = response_json['state']
        del(response_json['code'])
        logging.info('Spark Job status: ' + job_status)
        logging.info("Response: {}".format(pformat(response_json)))
        if 'progress' in statement_response.json():
            logging.info('Progress: ' + str(response_json['progress']))

        if job_status == 'idle':
            raise ValueError("track_spark_job error. Looks like you have passed spark session headers for the second parameter. "+
                             "Pass in spark job response headers instead.")

        if job_status != 'available':
            time.sleep(5)
            
    final_job_status = response_json['output']['status']

    # Get the logs
    log_lines = requests.get(session_url + '/log', 
                             headers={'Content-Type': 'application/json'}).json()['log']
    logging.info("Log from the cluster:\n{}".format("\n".join(log_lines)))
    logging.info('Final job Status: ' + final_job_status)

    if final_job_status == 'error':
        logging.info('Statement exception: ' + statement_response.json()['output']['evalue'])
        for trace in statement_response.json()['output']['traceback']:
            logging.info(trace)
        raise ValueError('Stopped because the final job status was "error".')
    
    return (final_job_status, log_lines)
# ------------


# Kill Spark Session
# ------------
def kill_spark_session(master_dns, session_headers, port=8998):
    session_url = spark_url(master_dns, location=session_headers['Location'], port=port)
    requests.delete(session_url, headers={'Content-Type': 'application/json'})
# ------------


# Delete Cluster
# ------------
def delete_cluster(emr_client, cluster_id):
    try:
        response = emr_client.terminate_job_flows(JobFlowIds=[cluster_id])
        
        cluster_removed = False
        while cluster_removed == False:
            if is_cluster_terminated(emr_client, cluster_id):
                cluster_removed = True
            else:
                state = get_cluster_status(emr_client, cluster_id)
                logging.info("Cluster {} has not been terminated (Current cluster state: {}). waiting until the status is TERMINATED...". \
                             format(cluster_id, state))
                time.sleep(10)
                
        print('Cluster {} Deleted'.format(cluster_id))
    except ClientError as e:
        print(e)
# ------------


# Delete Security Groups
# ------------
def delete_security_group(ec2_client, sgid):
    # Delete security group
    try:
        ec2res = boto3.resource('ec2')
        sg = ec2res.SecurityGroup(sgid)
        if len(sg.ip_permissions) > 0:
            for ip_permission in sg.ip_permissions:
                for group_pair in ip_permission['UserIdGroupPairs']:
                    if 'GroupName' in group_pair:
                        del(group_pair['GroupName'])
            sg.revoke_ingress(IpPermissions=sg.ip_permissions)
        response = ec2_client.delete_security_group(GroupId=sgid)
        logging.info('Security Group {} Deleted'.format(sgid))
    except ClientError as e:
        logging.error(e)
# ------------
