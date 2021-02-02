import logging
import os
import sys
import pysftp
import paramiko
from git import Repo
from jsonpath_ng import jsonpath, parse
import boto3
import requests
from tqdm import tqdm


def viewBar_no_tqdm(a, b):
    res = a / int(b) * 100
    sys.stdout.write("\r    Complete precent: %.2f %%" % (res))
    sys.stdout.flush()


def copyFileToRemoteSetup(
    server_public_ip, username, private_key, local_file, remote_file
):
    logging.info(
        "\tCopying local file {} to remote server {}".format(local_file, remote_file)
    )
    cnopts = pysftp.CnOpts()
    cnopts.hostkeys = None
    srv = pysftp.Connection(
        host=server_public_ip, username=username, private_key=private_key, cnopts=cnopts
    )
    srv.put(local_file, remote_file, callback=viewBar_no_tqdm)
    srv.close()
    logging.info("")


def getFileFromRemoteSetup(
    server_public_ip, username, private_key, local_file, remote_file
):
    logging.info(
        "\Retrieving remote file {} from remote server {} ".format(
            remote_file, server_public_ip
        )
    )
    cnopts = pysftp.CnOpts()
    cnopts.hostkeys = None
    srv = pysftp.Connection(
        host=server_public_ip, username=username, private_key=private_key, cnopts=cnopts
    )
    srv.get(remote_file, local_file, callback=viewBar_no_tqdm)
    srv.close()
    logging.info("")


def executeRemoteCommands(server_public_ip, username, private_key, commands):
    res = []
    k = paramiko.RSAKey.from_private_key_file(private_key)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    logging.info("Connecting to remote server {}".format(server_public_ip))
    c.connect(hostname=server_public_ip, username=username, pkey=k)
    logging.info("Connected to remote server {}".format(server_public_ip))
    for command in commands:
        logging.info('Executing remote command "{}"'.format(command))
        stdin, stdout, stderr = c.exec_command(command)
        stdout = stdout.readlines()
        stderr = stderr.readlines()
        res.append([stdout, stderr])
    c.close()
    return res


def checkDatasetRemoteRequirements(
    benchmark_config, server_public_ip, username, private_key, remote_dataset_file
):
    for k in benchmark_config["dbconfig"]:
        if "dataset" in k:
            dataset = k["dataset"]
    if dataset is not None:
        copyFileToRemoteSetup(
            server_public_ip,
            username,
            private_key,
            dataset,
            remote_dataset_file,
        )


def setupRemoteEnviroment(
    tf, tf_github_sha, tf_github_actor, tf_setup_name, tf_github_repo, tf_redis_module
):
    return_code, stdout, stderr = tf.init(capture_output=True)
    return_code, stdout, stderr = tf.refresh()
    tf_output = tf.output()
    server_private_ip = tf_output["server_private_ip"]["value"][0]
    server_public_ip = tf_output["server_public_ip"]["value"][0]
    client_private_ip = tf_output["client_private_ip"]["value"][0]
    client_public_ip = tf_output["client_public_ip"]["value"][0]
    if (
        server_private_ip is not None
        or server_public_ip is not None
        or client_private_ip is not None
        or client_public_ip is not None
    ):
        logging.warning("Destroying previous setup")
        tf.destroy()
    return_code, stdout, stderr = tf.apply(
        skip_plan=True,
        capture_output=False,
        refresh=True,
        var={
            "github_sha": tf_github_sha,
            "github_actor": tf_github_actor,
            "setup_name": tf_setup_name,
            "github_repo": tf_github_repo,
            "redis_module": tf_redis_module,
        },
    )
    tf_output = tf.output()
    server_private_ip = tf_output["server_private_ip"]["value"][0]
    server_public_ip = tf_output["server_public_ip"]["value"][0]
    server_plaintext_port = 6379
    client_private_ip = tf_output["client_private_ip"]["value"][0]
    client_public_ip = tf_output["client_public_ip"]["value"][0]
    username = "ubuntu"
    return (
        return_code,
        username,
        server_private_ip,
        server_public_ip,
        server_plaintext_port,
        client_private_ip,
        client_public_ip,
    )


def prepareBenchmarkCommand(
    server_private_ip, server_plaintext_port, benchmark_config, results_file
):
    queries_str = ""
    for k in benchmark_config["queries"]:
        query = k["q"]
        queries_str += ' -query "{}"'.format(query)
        if "ratio" in k:
            queries_str += " -query-ratio {}".format(k["ratio"])
    for k in benchmark_config["clientconfig"]:
        if "graph" in k:
            queries_str += " -graph-key {}".format(k["graph"])
        if "clients" in k:
            queries_str += " -c {}".format(k["clients"])
        if "requests" in k:
            queries_str += " -n {}".format(k["requests"])
        if "rps" in k:
            queries_str += " -rps {}".format(k["rps"])
    queries_str += " -h {}".format(server_private_ip)
    queries_str += " -p {}".format(server_plaintext_port)

    queries_str += " -json-out-file {}".format(results_file)
    logging.info(
        "Running the benchmark with the following parameters: {}".format(queries_str)
    )
    return queries_str


def spinUpRemoteRedis(
    benchmark_config,
    server_public_ip,
    username,
    private_key,
    local_module_file,
    remote_module_file,
    remote_dataset_file,
):

    # copy the rdb to DB machine
    dataset = None
    checkDatasetRemoteRequirements(
        benchmark_config,
        server_public_ip,
        username,
        private_key,
        remote_dataset_file,
    )

    # copy the module to the DB machine
    copyFileToRemoteSetup(
        server_public_ip,
        username,
        private_key,
        local_module_file,
        remote_module_file,
    )
    executeRemoteCommands(
        server_public_ip,
        username,
        private_key,
        ["chmod 755 {}".format(remote_module_file)],
    )
    # start redis-server
    commands = [
        "redis-server --dir /tmp/ --daemonize yes --protected-mode no --loadmodule {}".format(
            remote_module_file
        )
    ]
    executeRemoteCommands(server_public_ip, username, private_key, commands)


def setupRemoteBenchmark(
    client_public_ip, username, private_key, redisbenchmark_go_link
):
    commands = [
        "wget {} -q -O /tmp/redisgraph-benchmark-go".format(redisbenchmark_go_link),
        "chmod 755 /tmp/redisgraph-benchmark-go",
    ]
    executeRemoteCommands(client_public_ip, username, private_key, commands)


def runRemoteBenchmark(
    client_public_ip,
    username,
    private_key,
    server_private_ip,
    server_plaintext_port,
    benchmark_config,
    remote_results_file,
    local_results_file,
):
    queries_str = prepareBenchmarkCommand(
        server_private_ip, server_plaintext_port, benchmark_config, remote_results_file
    )
    commands = ["/tmp/redisgraph-benchmark-go {}".format(queries_str)]
    executeRemoteCommands(client_public_ip, username, private_key, commands)
    logging.info("Extracting the benchmark results")
    getFileFromRemoteSetup(
        client_public_ip,
        username,
        private_key,
        local_results_file,
        remote_results_file,
    )


def extract_git_vars():
    github_repo = Repo("{}/../..".format(os.getcwd()))
    github_repo_name = github_repo.remotes[0].config_reader.get("url")
    github_sha = github_repo.head.object.hexsha
    github_actor = github_repo.config_reader().get_value("user", "name")
    return github_repo_name, github_sha, github_actor


def validateResultExpectations(benchmark_config, results_dict, result):
    for expectation in benchmark_config["expectations"]:
        for comparison_mode, rules in expectation.items():
            for jsonpath, expected_value in rules.items():
                jsonpath_expr = parse(jsonpath)
                actual_value = float(jsonpath_expr.find(results_dict)[0].value)
                expected_value = float(expected_value)
                if comparison_mode == "eq":
                    if actual_value != expected_value:
                        result &= False
                        logging.error(
                            "Condition {} {} {} is False. Failing test expectations".format(
                                actual_value,
                                comparison_mode,
                                expected_value,
                            )
                        )
                if comparison_mode == "le":
                    if actual_value > expected_value:
                        result &= False
                        logging.error(
                            "Condition {} {} {} is False. Failing test expectations".format(
                                actual_value,
                                comparison_mode,
                                expected_value,
                            )
                        )
                if comparison_mode == "ge":
                    if actual_value < expected_value:
                        result &= False
                        logging.error(
                            "Condition {} {} {} is False. Failing test expectations".format(
                                actual_value,
                                comparison_mode,
                                expected_value,
                            )
                        )
    return result


def upload_artifacts_to_s3(artifacts, s3_bucket_name, s3_bucket_path):
    logging.info("Uploading results to s3")
    s3 = boto3.resource("s3")
    bucket = s3.Bucket(s3_bucket_name)
    progress = tqdm(unit="files", total=len(artifacts))
    for input in artifacts:
        object_key = "{bucket_path}{filename}".format(
            bucket_path=s3_bucket_path, filename=input
        )
        bucket.upload_file(input, object_key)
        object_acl = s3.ObjectAcl(s3_bucket_name, object_key)
        response = object_acl.put(ACL="public-read")
        progress.update()
    progress.close()