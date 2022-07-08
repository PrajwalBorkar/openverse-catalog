"""
# Data Refresh TaskGroup Factory
This file generates the data refresh TaskGroup using a factory function.
This TaskGroup initiates a data refresh for a given media type and awaits the
success or failure of the refresh. Importantly, it is also configured to
ensure that no two remote data refreshes can run concurrently, as required by
the server.

A data refresh occurs on the data refresh server in the openverse-api project.
This is a task which imports data from the upstream Catalog database into the
API, copies contents to a new Elasticsearch index, and makes the index "live".
This process is necessary to make new content added to the Catalog by our
provider DAGs available on the frontend. You can read more in the [README](
https://github.com/WordPress/openverse-api/blob/main/ingestion_server/README.md
)

The TaskGroup generated by this factory allows us to schedule those refreshes through
Airflow. Since no two refreshes can run simultaneously, all tasks are initially
funneled through a special `data_refresh` pool with a single worker slot. To ensure
that tasks run in an acceptable order (ie the trigger step for one DAG cannot run if a
previously triggered refresh is still running), each DAG has the following
steps:

1. The `wait_for_data_refresh` step uses a custom Sensor that will wait until
none of the `external_dag_ids` (corresponding to the other data refresh DAGs)
are 'running'. A DAG is considered to be 'running' if it is itself in the
RUNNING state __and__ its own `wait_for_data_refresh` step has completed
successfully. The Sensor suspends itself and frees up the worker slot if
another data refresh DAG is running.

2. The `trigger_data_refresh` step then triggers the data refresh by POSTing
to the `/task` endpoint on the data refresh server with relevant data. A
successful response will include the `status_check` url used to check on the
status of the refresh, which is passed on to the next task via XCom.

3. Finally the `wait_for_data_refresh` task waits for the data refresh to be
complete by polling the `status_url`. Note this task does not need to be
able to suspend itself and free the worker slot, because we want to lock the
entire pool on waiting for a particular data refresh to run.

You can find more background information on this process in the following
issues and related PRs:

- [[Feature] Data refresh orchestration DAG](
https://github.com/WordPress/openverse-catalog/issues/353)
"""
import json
import logging
import os
import uuid
from typing import Sequence
from urllib.parse import urlparse

from airflow.exceptions import AirflowException
from airflow.models.baseoperator import chain
from airflow.providers.http.operators.http import SimpleHttpOperator
from airflow.providers.http.sensors.http import HttpSensor
from airflow.utils.task_group import TaskGroup
from common.constants import XCOM_PULL_TEMPLATE
from common.sensors.single_run_external_dags_sensor import SingleRunExternalDAGsSensor
from data_refresh.data_refresh_types import DataRefresh
from requests import Response


logger = logging.getLogger(__name__)


DATA_REFRESH_POOL = "data_refresh"


def response_filter_data_refresh(response: Response) -> str:
    """
    Filter for the `trigger_data_refresh` task, used to grab the endpoint needed
    to poll for the status of the triggered data refresh. This information will
    then be available via XCom in the downstream tasks.
    """
    status_check_url = response.json()["status_check"]
    return urlparse(status_check_url).path


def response_check_wait_for_completion(response: Response) -> bool:
    """
    Response check to the `wait_for_completion` Sensor. Processes the response to
    determine whether the task can complete.
    """
    data = response.json()

    if data["active"]:
        # The data refresh is still running. Poll again later.
        return False

    if data["error"]:
        raise AirflowException("Error triggering data refresh.")

    logger.info(
        f"Data refresh done with {data['percent_completed']}% \
        completed."
    )
    return True


def create_data_refresh_task_group(
    data_refresh: DataRefresh, external_dag_ids: Sequence[str]
):
    """
    This factory method instantiates a DAG that will run the data refresh for
    the given `media_type`.

    A data refresh runs for a given media type in the API DB. It refreshes popularity
    data for that type, imports the data from the upstream DB in the Catalog, reindexes
    the data, and updates and reindex Elasticsearch.

    A data refresh can only be performed for one media type at a time, so the DAG
    must also use a Sensor to make sure that no two data refresh tasks run
    concurrently.

    It is intended that the data_refresh tasks, or at least the initial
    `wait_for_data_refresh` tasks, should be run in a custom pool with 1 worker
    slot. This enforces that no two `wait_for_data_refresh` tasks can start
    concurrently and enter a race condition.

    Required Arguments:

    data_refresh:     dataclass containing configuration information for the
                      DAG
    external_dag_ids: list of ids of the other data refresh DAGs. This DAG
                      will not run concurrently with any dependent DAG.
    """

    poke_interval = int(os.getenv("DATA_REFRESH_POKE_INTERVAL", 60 * 15))

    with TaskGroup(group_id="data_refresh") as data_refresh_group:
        # Wait to ensure that no other Data Refresh DAGs are running.
        wait_for_data_refresh = SingleRunExternalDAGsSensor(
            task_id="wait_for_data_refresh",
            external_dag_ids=external_dag_ids,
            check_existence=True,
            poke_interval=poke_interval,
            mode="reschedule",
            pool=DATA_REFRESH_POOL,
        )

        # This UUID is the suffix for the new index created as a result of this refresh.
        index_suffix = str(uuid.uuid4())

        def _get_http_operator(task_id: str, post_data: dict) -> SimpleHttpOperator:
            """
            Get a ``SimpleHttpOperator`` instance that is configured to make a POST
            request with the given POST data.

            :param task_id: the name of the task associated with the operator
            :param post_data: the JSON data to send to the ingestion server
            """

            return SimpleHttpOperator(
                task_id=task_id,
                http_conn_id="data_refresh",
                endpoint="task",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps(post_data),
                response_check=lambda response: response.status_code == 202,
                response_filter=response_filter_data_refresh,
            )

        def _get_http_sensor(task_id: str, endpoint: str) -> HttpSensor:
            """
            Get an ``HttpSensor`` instance that waits for a given  ingestion server task
            to be completed. The trigger task status can be observed by polling an
            endpoint.

            :param task_id: the name of the task associated with the sensor
            :endpoint: the REST endpoint for tracking the status of the triggered task
            """

            return HttpSensor(
                task_id=task_id,
                http_conn_id="data_refresh",
                endpoint=endpoint,
                method="GET",
                response_check=response_check_wait_for_completion,
                mode="reschedule",
                poke_interval=poke_interval,
                timeout=data_refresh.data_refresh_timeout,
            )

        tasks = [wait_for_data_refresh]
        action_data_map: dict[str, dict] = {
            "ingest_upstream": {},
            "point_alias": {"alias": data_refresh.media_type},
        }
        for action, action_post_data in action_data_map.items():
            with TaskGroup(group_id=action) as task_group:
                trigger = _get_http_operator(
                    task_id=f"trigger_{action}",
                    post_data=action_post_data
                    | {
                        "model": data_refresh.media_type,
                        "action": action.upper(),
                        "index_suffix": index_suffix,
                    },
                )
                waiter = _get_http_sensor(
                    task_id=f"wait_for_{action}",
                    endpoint=XCOM_PULL_TEMPLATE.format(trigger.task_id, "return_value"),
                )
                trigger >> waiter

            tasks.append(task_group)

        # ``tasks`` contains the following tasks:
        # wait_for_data_refresh
        # └─ ingest_upstream (trigger_ingest_upstream + wait_for_ingest_upstream)
        #    └─ point_alias (trigger_point_alias + wait_for_point_alias)
        chain(*tasks)

    return data_refresh_group
