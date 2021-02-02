import yaml
import pathlib
from python_terraform import Terraform
import argparse
import os
import logging
import json
import datetime as dt


from common import (
    setupRemoteEnviroment,
    spinUpRemoteRedis,
    setupRemoteBenchmark,
    runRemoteBenchmark,
    extract_git_vars,
    validateResultExpectations,
    upload_artifacts_to_s3,
)

# logging settings
logging.basicConfig(
    format="%(asctime)s %(levelname)-4s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)

# internal aux vars
redisbenchmark_go_link = "https://s3.amazonaws.com/benchmarks.redislabs/redisgraph/redisgraph-benchmark-go/unstable/redisgraph-benchmark-go_linux_amd64"
remote_dataset_file = "/tmp/dump.rdb"
remote_module_file = "/tmp/redisgraph.so"
local_results_file = "./benchmark-result.json"
remote_results_file = "/tmp/benchmark-result.json"
private_key = "/tmp/benchmarks.redislabs.redisgraph.pem"

parser = argparse.ArgumentParser(
    description="RedisGraph remote performance tester.",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
default_terraform_bin = os.getenv("TERRAFORM_BIN_PATH", "terraform")

parser.add_argument("--github_actor", type=str, default=None)
parser.add_argument("--github_repo", type=str, default=None)
parser.add_argument("--github_sha", type=str, default=None)
parser.add_argument("--redis_module", type=str, default="RedisGraph")
parser.add_argument("--terraform_bin_path", type=str, default=default_terraform_bin)
parser.add_argument("--module_path", type=str, default="./../../src/redisgraph.so")
parser.add_argument("--setup_name_sufix", type=str, default="")
parser.add_argument(
    "--s3_bucket_name",
    type=str,
    default="ci.benchmarks.redislabs",
    help="S3 bucket name.",
)
parser.add_argument(
    "--upload_results_s3",
    default=False,
    action="store_true",
    help="uploads the result files and configuration file to public ci.benchmarks.redislabs bucket. Proper credentials are required",
)

args = parser.parse_args()

tf_bin_path = args.terraform_bin_path
tf_github_actor = args.github_actor
tf_github_repo = args.github_repo
tf_github_sha = args.github_sha
if tf_github_actor is None:
    github_repo_name, github_sha, github_actor = extract_git_vars()
    tf_github_actor = github_actor
if tf_github_repo is None:
    github_repo_name, github_sha, github_actor = extract_git_vars()
    tf_github_repo = github_repo_name
if tf_github_sha is None:
    github_repo_name, github_sha, github_actor = extract_git_vars()
    tf_github_sha = github_sha

tf_redis_module = args.redis_module
tf_setup_name_sufix = "{}-{}".format(args.setup_name_sufix, tf_github_sha)
s3_bucket_name = args.s3_bucket_name
local_module_file = args.module_path

EC2_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID", None)
EC2_PRIVATE_PEM = os.getenv("EC2_PRIVATE_PEM", None)
EC2_REGION = os.getenv("AWS_DEFAULT_REGION", None)
EC2_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", None)

if EC2_ACCESS_KEY is None or EC2_ACCESS_KEY == "":
    logging.error("missing required AWS_ACCESS_KEY_ID env variable")
    exit(1)
if EC2_PRIVATE_PEM is None or EC2_PRIVATE_PEM == "":
    logging.error("missing required EC2_PRIVATE_PEM env variable")
    exit(1)
if EC2_REGION is None or EC2_REGION == "":
    logging.error("missing required AWS_DEFAULT_REGION env variable")
    exit(1)
if EC2_SECRET_KEY is None or EC2_SECRET_KEY == "":
    logging.error("missing required AWS_SECRET_ACCESS_KEY env variable")
    exit(1)

logging.info("Using the following vars on terraform deployment:")
logging.info("\tterraform bin path: {}".format(tf_bin_path))
logging.info("\tgithub_actor: {}".format(tf_github_actor))
logging.info("\tgithub_repo: {}".format(tf_github_repo))
logging.info("\tgithub_sha: {}".format(tf_github_sha))
logging.info("\tredis_module: {}".format(tf_redis_module))
logging.info("\tsetup_name sufix: {}".format(tf_setup_name_sufix))

# Iterate over each benchmark file in folder
files = pathlib.Path().glob("*.yml")
remote_benchmark_setups = pathlib.Path().glob("./aws/tf-*")

with open(private_key, "w") as tmp_private_key_file:
    tmp_private_key_file.write(EC2_PRIVATE_PEM)


def get_run_full_filename(test_name, deployment_type, git_sha, start_time_str):
    benchmark_output_filename = (
        "{time_str}-{test_name}-{deployment_type}-{git_sha}.json".format(
            time_str=start_time_str,
            test_name=test_name,
            deployment_type=deployment_type,
            git_sha=git_sha,
        )
    )
    return benchmark_output_filename


return_code = 0
for f in files:
    with open(f, "r") as stream:
        benchmark_config = yaml.safe_load(stream)
        test_name = benchmark_config["name"]
        s3_bucket_path = "{project}/results/{test_name}/".format(
            project=tf_redis_module, test_name=test_name
        )
        s3_uri = "https://s3.amazonaws.com/{bucket_name}/{bucket_path}".format(
            bucket_name=s3_bucket_name, bucket_path=s3_bucket_path
        )

        if "ci" in benchmark_config and "terraform" in benchmark_config["ci"]:
            for remote_setup in benchmark_config["ci"]["terraform"]:
                # Setup Infra
                logging.info(
                    "Deploying test defined in {} on AWS using {}".format(
                        f, remote_setup
                    )
                )
                tf_setup_name = "{}{}".format(remote_setup, tf_setup_name_sufix)
                logging.info("Using full setup name: {}".format(tf_setup_name))
                tf = Terraform(
                    working_dir=remote_setup,
                    terraform_bin_path=tf_bin_path,
                )
                (
                    return_code,
                    username,
                    server_private_ip,
                    server_public_ip,
                    server_plaintext_port,
                    client_private_ip,
                    client_public_ip,
                ) = setupRemoteEnviroment(
                    tf,
                    tf_github_sha,
                    tf_github_actor,
                    tf_setup_name,
                    tf_github_repo,
                    tf_redis_module,
                )

                # setup RedisGraph
                spinUpRemoteRedis(
                    benchmark_config,
                    server_public_ip,
                    username,
                    private_key,
                    local_module_file,
                    remote_module_file,
                    remote_dataset_file,
                )

                # setup the benchmark
                setupRemoteBenchmark(
                    client_public_ip, username, private_key, redisbenchmark_go_link
                )
                start_time = dt.datetime.now()
                start_time_str = start_time.strftime("%Y-%m-%d-%H-%M-%S")
                local_benchmark_output_filename = get_run_full_filename(
                    test_name, "", tf_github_sha, start_time_str
                )
                logging.info(
                    "Will store benchmark json output to local file {}".format(
                        local_benchmark_output_filename
                    )
                )
                # run the benchmark
                runRemoteBenchmark(
                    client_public_ip,
                    username,
                    private_key,
                    server_private_ip,
                    server_plaintext_port,
                    benchmark_config,
                    remote_results_file,
                    local_benchmark_output_filename,
                )

                # check requirements
                result = True
                if "expectations" in benchmark_config:
                    results_dict = None
                    with open(local_benchmark_output_filename, "r") as json_file:
                        results_dict = json.load(json_file)
                    result = validateResultExpectations(
                        benchmark_config, results_dict, result
                    )
                    if result != True:
                        return_code &= 1

                if args.upload_results_s3:
                    logging.info("Uploading results to s3")
                    artifacts = [local_benchmark_output_filename]
                    upload_artifacts_to_s3(artifacts, s3_bucket_name, s3_bucket_path)

                # tear-down
                logging.info("Tearing down setup")
                tf_output = tf.destroy()
                logging.info("Tear-down completed")

exit(return_code)
