import asyncio
import os
import pprint
import time
from subprocess import list2cmdline
from typing import Optional, Tuple, Union

import click

import ray._private.ray_constants as ray_constants
from ray._private.storage import _load_class
from ray.autoscaler._private.cli_logger import add_click_logging_options, cf, cli_logger
from ray.dashboard.modules.dashboard_sdk import parse_runtime_env_args
from ray.job_submission import JobStatus, JobSubmissionClient
from ray.util.annotations import PublicAPI
from ray._private.utils import parse_resources_json


def _get_sdk_client(
    address: Optional[str], create_cluster_if_needed: bool = False
) -> JobSubmissionClient:
    client = JobSubmissionClient(address, create_cluster_if_needed)
    client_address = client.get_address()
    cli_logger.labeled_value("Job submission server address", client_address)
    return client


def _log_big_success_msg(success_msg):
    cli_logger.newline()
    cli_logger.success("-" * len(success_msg))
    cli_logger.success(success_msg)
    cli_logger.success("-" * len(success_msg))
    cli_logger.newline()


def _log_big_error_msg(success_msg):
    cli_logger.newline()
    cli_logger.error("-" * len(success_msg))
    cli_logger.error(success_msg)
    cli_logger.error("-" * len(success_msg))
    cli_logger.newline()


def _log_job_status(client: JobSubmissionClient, job_id: str):
    info = client.get_job_info(job_id)
    if info.status == JobStatus.SUCCEEDED:
        _log_big_success_msg(f"Job '{job_id}' succeeded")
    elif info.status == JobStatus.STOPPED:
        cli_logger.warning(f"Job '{job_id}' was stopped")
    elif info.status == JobStatus.FAILED:
        _log_big_error_msg(f"Job '{job_id}' failed")
        if info.message is not None:
            cli_logger.print(f"Status message: {info.message}", no_format=True)
    else:
        # Catch-all.
        cli_logger.print(f"Status for job '{job_id}': {info.status}")
        if info.message is not None:
            cli_logger.print(f"Status message: {info.message}", no_format=True)


async def _tail_logs(client: JobSubmissionClient, job_id: str):
    async for lines in client.tail_job_logs(job_id):
        print(lines, end="")

    _log_job_status(client, job_id)


@click.group("job")
def job_cli_group():
    pass


@job_cli_group.command()
@click.option(
    "--address",
    type=str,
    default=None,
    required=False,
    help=(
        "Address of the Ray cluster to connect to. Can also be specified "
        "using the RAY_ADDRESS environment variable."
    ),
)
@click.option(
    "--job-id",
    type=str,
    default=None,
    required=False,
    help=("DEPRECATED: Use -- submission-id instead."),
)
@click.option(
    "--submission-id",
    type=str,
    default=None,
    required=False,
    help=(
        "Submission ID to specify for the job. "
        "If not provided, one will be generated."
    ),
)
@click.option(
    "--runtime-env",
    type=str,
    default=None,
    required=False,
    help="Path to a local YAML file containing a runtime_env definition.",
)
@click.option(
    "--runtime-env-json",
    type=str,
    default=None,
    required=False,
    help="JSON-serialized runtime_env dictionary.",
)
@click.option(
    "--working-dir",
    type=str,
    default=None,
    required=False,
    help=(
        "Directory containing files that your job will run in. Can be a "
        "local directory or a remote URI to a .zip file (S3, GS, HTTP). "
        "If specified, this overrides the option in --runtime-env."
    ),
)
@click.option(
    "--entrypoint-num-cpus",
    required=False,
    type=float,
    help="the quantity of CPU cores to reserve for the entrypoint command, "
    "separately from any tasks or actors that are launched by it",
)
@click.option(
    "--entrypoint-num-gpus",
    required=False,
    type=float,
    help="the quantity of GPUs to reserve for the entrypoint command, "
    "separately from any tasks or actors that are launched by it",
)
@click.option(
    "--entrypoint-resources",
    required=False,
    type=str,
    help="a JSON-serialized dictionary mapping resource name to resource quantity "
    "describing resources to reserve for the entrypoint command, "
    "separately from any tasks or actors that are launched by it",
)
@click.option(
    "--no-wait",
    is_flag=True,
    type=bool,
    default=False,
    help="If set, will not stream logs and wait for the job to exit.",
)
@add_click_logging_options
@click.argument("entrypoint", nargs=-1, required=True, type=click.UNPROCESSED)
@PublicAPI
def submit(
    address: Optional[str],
    job_id: Optional[str],
    submission_id: Optional[str],
    runtime_env: Optional[str],
    runtime_env_json: Optional[str],
    working_dir: Optional[str],
    entrypoint: Tuple[str],
    entrypoint_num_cpus: Optional[Union[int, float]],
    entrypoint_num_gpus: Optional[Union[int, float]],
    entrypoint_resources: Optional[str],
    no_wait: bool,
):
    """Submits a job to be run on the cluster.

    Example:
        ray job submit -- python my_script.py --arg=val
    """

    if job_id:
        cli_logger.warning(
            "--job-id option is deprecated. Please use --submission-id instead."
        )
    if entrypoint_resources is not None:
        entrypoint_resources = parse_resources_json(
            entrypoint_resources, cli_logger, cf, command_arg="entrypoint-resources"
        )

    submission_id = submission_id or job_id

    if ray_constants.RAY_JOB_SUBMIT_HOOK in os.environ:
        # Submit all args as **kwargs per the JOB_SUBMIT_HOOK contract.
        _load_class(os.environ[ray_constants.RAY_JOB_SUBMIT_HOOK])(
            address=address,
            job_id=submission_id,
            submission_id=submission_id,
            runtime_env=runtime_env,
            runtime_env_json=runtime_env_json,
            working_dir=working_dir,
            entrypoint=entrypoint,
            entrypoint_num_cpus=entrypoint_num_cpus,
            entrypoint_num_gpus=entrypoint_num_gpus,
            entrypoint_resources=entrypoint_resources,
            no_wait=no_wait,
        )

    client = _get_sdk_client(address, create_cluster_if_needed=True)

    final_runtime_env = parse_runtime_env_args(
        runtime_env=runtime_env,
        runtime_env_json=runtime_env_json,
        working_dir=working_dir,
    )
    job_id = client.submit_job(
        entrypoint=list2cmdline(entrypoint),
        submission_id=submission_id,
        runtime_env=final_runtime_env,
        entrypoint_num_cpus=entrypoint_num_cpus,
        entrypoint_num_gpus=entrypoint_num_gpus,
        entrypoint_resources=entrypoint_resources,
    )

    _log_big_success_msg(f"Job '{job_id}' submitted successfully")

    with cli_logger.group("Next steps"):
        cli_logger.print("Query the logs of the job:")
        with cli_logger.indented():
            cli_logger.print(cf.bold(f"ray job logs {job_id}"))

        cli_logger.print("Query the status of the job:")
        with cli_logger.indented():
            cli_logger.print(cf.bold(f"ray job status {job_id}"))

        cli_logger.print("Request the job to be stopped:")
        with cli_logger.indented():
            cli_logger.print(cf.bold(f"ray job stop {job_id}"))

    cli_logger.newline()
    sdk_version = client.get_version()
    # sdk version 0 does not have log streaming
    if not no_wait:
        if int(sdk_version) > 0:
            cli_logger.print(
                "Tailing logs until the job exits " "(disable with --no-wait):"
            )
            asyncio.get_event_loop().run_until_complete(_tail_logs(client, job_id))
        else:
            cli_logger.warning(
                "Tailing logs is not enabled for job sdk client version "
                f"{sdk_version}. Please upgrade Ray to the latest version "
                "for this feature."
            )


@job_cli_group.command()
@click.option(
    "--address",
    type=str,
    default=None,
    required=False,
    help=(
        "Address of the Ray cluster to connect to. Can also be specified "
        "using the RAY_ADDRESS environment variable."
    ),
)
@click.argument("job-id", type=str)
@add_click_logging_options
@PublicAPI(stability="beta")
def status(address: Optional[str], job_id: str):
    """Queries for the current status of a job.

    Example:
        ray job status <my_job_id>
    """
    client = _get_sdk_client(address)
    _log_job_status(client, job_id)


@job_cli_group.command()
@click.option(
    "--address",
    type=str,
    default=None,
    required=False,
    help=(
        "Address of the Ray cluster to connect to. Can also be specified "
        "using the RAY_ADDRESS environment variable."
    ),
)
@click.option(
    "--no-wait",
    is_flag=True,
    type=bool,
    default=False,
    help="If set, will not wait for the job to exit.",
)
@click.argument("job-id", type=str)
@add_click_logging_options
@PublicAPI(stability="beta")
def stop(address: Optional[str], no_wait: bool, job_id: str):
    """Attempts to stop a job.

    Example:
        ray job stop <my_job_id>
    """
    client = _get_sdk_client(address)
    cli_logger.print(f"Attempting to stop job {job_id}")
    client.stop_job(job_id)

    if no_wait:
        return
    else:
        cli_logger.print(
            f"Waiting for job '{job_id}' to exit " f"(disable with --no-wait):"
        )

    while True:
        status = client.get_job_status(job_id)
        if status in {JobStatus.STOPPED, JobStatus.SUCCEEDED, JobStatus.FAILED}:
            _log_job_status(client, job_id)
            break
        else:
            cli_logger.print(f"Job has not exited yet. Status: {status}")
            time.sleep(1)


@job_cli_group.command()
@click.option(
    "--address",
    type=str,
    default=None,
    required=False,
    help=(
        "Address of the Ray cluster to connect to. Can also be specified "
        "using the RAY_ADDRESS environment variable."
    ),
)
@click.argument("job-id", type=str)
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    type=bool,
    default=False,
    help="If set, follow the logs (like `tail -f`).",
)
@add_click_logging_options
@PublicAPI(stability="beta")
def logs(address: Optional[str], job_id: str, follow: bool):
    """Gets the logs of a job.

    Example:
        ray job logs <my_job_id>
    """
    client = _get_sdk_client(address)
    sdk_version = client.get_version()
    # sdk version 0 did not have log streaming
    if follow:
        if int(sdk_version) > 0:
            asyncio.get_event_loop().run_until_complete(_tail_logs(client, job_id))
        else:
            cli_logger.warning(
                "Tailing logs is not enabled for the Jobs SDK client version "
                f"{sdk_version}. Please upgrade Ray to latest version "
                "for this feature."
            )
    else:
        # Set no_format to True because the logs may have unescaped "{" and "}"
        # and the CLILogger calls str.format().
        cli_logger.print(client.get_job_logs(job_id), end="", no_format=True)


@job_cli_group.command()
@click.option(
    "--address",
    type=str,
    default=None,
    required=False,
    help=(
        "Address of the Ray cluster to connect to. Can also be specified "
        "using the RAY_ADDRESS environment variable."
    ),
)
@add_click_logging_options
@PublicAPI(stability="beta")
def list(address: Optional[str]):
    """Lists all running jobs and their information.

    Example:
        ray job list
    """
    client = _get_sdk_client(address)
    # Set no_format to True because the logs may have unescaped "{" and "}"
    # and the CLILogger calls str.format().
    cli_logger.print(pprint.pformat(client.list_jobs()), no_format=True)
