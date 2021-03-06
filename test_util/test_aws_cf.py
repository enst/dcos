#!/usr/bin/env python3
"""Deploys DC/OS AWS CF template and then runs integration_test.py

The following environment variables control test procedure:

DCOS_TEMPLATE_URL: string
    The template to be used for deployment testing

CI_FLAGS: string (default=None)
    If provided, this string will be passed directly to py.test as in:
    py.test -vv CI_FLAGS integration_test.py

TEST_ADD_ENV_*: string (default=None)
    Any number of environment variables can be passed to integration_test.py if
    prefixed with 'TEST_ADD_ENV_'. The prefix will be removed before passing
"""
import logging
import os
import random
import stat
import string
import sys
from contextlib import closing

import test_util.aws
import test_util.test_runner
from gen.calc import calculate_environment_variable
from ssh.ssh_tunnel import SSHTunnel

LOGGING_FORMAT = '[%(asctime)s|%(name)s|%(levelname)s]: %(message)s'
logging.basicConfig(format=LOGGING_FORMAT, level=logging.DEBUG)
log = logging.getLogger(__name__)


def check_environment():
    """Test uses environment variables to play nicely with TeamCity config templates
    Grab all the environment variables here to avoid setting params all over

    Returns:
        object: generic object used for cleanly passing options through the test

    Raises:
        AssertionError: if any environment variables or resources are missing
            or do not conform
    """
    options = type('Options', (object,), {})()

    # Defaults
    options.ci_flags = os.getenv('CI_FLAGS', '')
    options.aws_region = os.getenv('DEFAULT_AWS_REGION', 'eu-central-1')

    options.variant = calculate_environment_variable('DCOS_VARIANT')
    options.template_url = calculate_environment_variable('DCOS_TEMPLATE_URL')
    options.aws_access_key_id = calculate_environment_variable('AWS_ACCESS_KEY_ID')
    options.aws_secret_access_key = calculate_environment_variable('AWS_SECRET_ACCESS_KEY')

    add_env = {}
    prefix = 'TEST_ADD_ENV_'
    for k, v in os.environ.items():
        if k.startswith(prefix):
            add_env[k.replace(prefix, '')] = v
    options.add_env = add_env
    options.pytest_dir = os.getenv('DCOS_PYTEST_DIR', '/opt/mesosphere/active/dcos-integration-test')
    options.pytest_cmd = os.getenv('DCOS_PYTEST_CMD', 'py.test -vv -rs ' + options.ci_flags)
    return options


def main():
    options = check_environment()

    random_identifier = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    unique_cluster_id = 'CF-integration-test-{}'.format(random_identifier)
    log.info('Spinning up AWS CloudFormation with ID: {}'.format(unique_cluster_id))
    bw = test_util.aws.BotoWrapper(
        region=options.aws_region,
        aws_access_key_id=options.aws_access_key_id,
        aws_secret_access_key=options.aws_secret_access_key)
    # TODO(mellenburg): use randomly generated keys this key is delivered by CI or user
    ssh_key_path = 'default_ssh_key'
    cf = test_util.aws.DcosCfSimple.create(
        stack_name=unique_cluster_id,
        template_url=options.template_url,
        private_agents=2,
        public_agents=1,
        admin_location='0.0.0.0/0',
        key_pair_name='default',
        boto_wrapper=bw)
    cf.wait_for_stack_creation()

    # key must be chmod 600 for test_runner to use
    os.chmod(ssh_key_path, stat.S_IREAD | stat.S_IWRITE)

    # Create custom SSH Runnner to help orchestrate the test
    ssh_user = 'core'
    remote_dir = '/home/core'

    master_ips = cf.get_master_ips()
    public_agent_ips = cf.get_public_agent_ips()
    private_agent_ips = cf.get_private_agent_ips()
    test_host = master_ips[0].public_ip
    log.info('Running integration test from: ' + test_host)
    master_list = [i.private_ip for i in master_ips]
    log.info('Master private IPs: ' + repr(master_list))
    agent_list = [i.private_ip for i in private_agent_ips]
    log.info('Private agent private IPs: ' + repr(agent_list))
    public_agent_list = [i.private_ip for i in public_agent_ips]
    log.info('Public agent private IPs: ' + repr(public_agent_list))

    log.info('To access this cluster, use the Mesosphere default shared AWS key '
             '(https://mesosphere.onelogin.com/notes/16670) and SSH with:\n'
             'ssh -i default_ssh_key {}@{}'.format(ssh_user, test_host))
    with closing(SSHTunnel(ssh_user, ssh_key_path, test_host)) as test_host_tunnel:
        # Allow docker use w/o sudo
        result = test_util.test_runner.integration_test(
            tunnel=test_host_tunnel,
            test_dir=remote_dir,
            region=options.aws_region,
            dcos_dns=master_list[0],
            master_list=master_list,
            agent_list=agent_list,
            public_agent_list=public_agent_list,
            provider='aws',
            test_dns_search=False,
            aws_access_key_id=options.aws_access_key_id,
            aws_secret_access_key=options.aws_secret_access_key,
            add_env=options.add_env,
            pytest_dir=options.pytest_dir,
            pytest_cmd=options.pytest_cmd)
    if result == 0:
        log.info('Test successsful! Deleting CloudFormation...')
        cf.delete()
    else:
        log.info('Test failed! VPC will remain alive for debugging')
    if options.ci_flags:
        result = 0  # Wipe the return code so that tests can be muted in CI
    sys.exit(result)

if __name__ == '__main__':
    main()
