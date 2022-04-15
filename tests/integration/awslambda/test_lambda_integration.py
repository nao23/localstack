import base64

base64
import json
import os
import time
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from localstack import config
from localstack.services.apigateway.helpers import path_based_url
from localstack.services.awslambda.lambda_api import (
    BATCH_SIZE_RANGES,
    INVALID_PARAMETER_VALUE_EXCEPTION,
)
from localstack.services.awslambda.lambda_utils import (
    LAMBDA_RUNTIME_PYTHON36,
    LAMBDA_RUNTIME_PYTHON37,
)
from localstack.utils import testutil
from localstack.utils.aws import aws_stack
from localstack.utils.common import retry, safe_requests, short_uid
from localstack.utils.strings import to_bytes
from localstack.utils.sync import poll_condition
from localstack.utils.testutil import check_expected_lambda_log_events_length, get_lambda_log_events

from .functions import lambda_integration
from .test_lambda import (
    TEST_LAMBDA_FUNCTION_PREFIX,
    TEST_LAMBDA_LIBS,
    TEST_LAMBDA_PYTHON,
    TEST_LAMBDA_PYTHON_ECHO,
    TEST_LAMBDA_PYTHON_UNHANDLED_ERROR,
    is_old_provider,
)

TEST_STAGE_NAME = "testing"
TEST_SNS_TOPIC_NAME = "sns-topic-1"

THIS_FOLDER = os.path.dirname(os.path.realpath(__file__))
TEST_LAMBDA_PARALLEL_FILE = os.path.join(THIS_FOLDER, "functions", "lambda_parallel.py")


lambda_role = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}
s3_lambda_permission = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "sqs:*",
                "dynamodb:DescribeStream",
                "dynamodb:GetRecords",
                "dynamodb:GetShardIterator",
                "dynamodb:ListStreams",
                "kinesis:DescribeStream",
                "kinesis:DescribeStreamSummary",
                "kinesis:GetRecords",
                "kinesis:GetShardIterator",
                "kinesis:ListShards",
                "kinesis:ListStreams",
                "kinesis:SubscribeToShard",
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            "Resource": ["*"],
        }
    ],
}


class TestSNSSubscription:
    def test_python_lambda_subscribe_sns_topic(
        self,
        create_lambda_function,
        sns_client,
        lambda_su_role,
        sns_topic,
        logs_client,
        lambda_client,
    ):
        function_name = f"{TEST_LAMBDA_FUNCTION_PREFIX}-{short_uid()}"
        permission_id = f"test-statement-{short_uid()}"

        lambda_creation_response = create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=LAMBDA_RUNTIME_PYTHON36,
            role=lambda_su_role,
        )
        lambda_arn = lambda_creation_response["CreateFunctionResponse"]["FunctionArn"]

        topic_arn = sns_topic["Attributes"]["TopicArn"]

        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=permission_id,
            Action="lambda:InvokeFunction",
            Principal="sns.amazonaws.com",
            SourceArn=topic_arn,
        )

        sns_client.subscribe(
            TopicArn=topic_arn,
            Protocol="lambda",
            Endpoint=lambda_arn,
        )

        subject = "[Subject] Test subject"
        message = "Hello world."
        sns_client.publish(TopicArn=topic_arn, Subject=subject, Message=message)

        events = retry(
            check_expected_lambda_log_events_length,
            retries=10,
            sleep=1,
            function_name=function_name,
            expected_length=1,
            regex_filter="Records.*Sns",
            logs_client=logs_client,
        )
        notification = events[0]["Records"][0]["Sns"]

        assert "Subject" in notification
        assert subject == notification["Subject"]


class TestSQSEventSourceMapping:
    def test_event_source_mapping_default_batch_size(
        self,
        create_lambda_function,
        lambda_client,
        sqs_client,
        sqs_create_queue,
        sqs_queue_arn,
        dynamodb_client,
        dynamodb_create_table,
        lambda_su_role,
    ):
        function_name = f"lambda_func-{short_uid()}"
        queue_name_1 = f"queue-{short_uid()}-1"
        queue_name_2 = f"queue-{short_uid()}-2"

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=LAMBDA_RUNTIME_PYTHON36,
            role=lambda_su_role,
        )

        queue_url_1 = sqs_create_queue(QueueName=queue_name_1)
        queue_arn_1 = sqs_queue_arn(queue_url_1)

        rs = lambda_client.create_event_source_mapping(
            EventSourceArn=queue_arn_1, FunctionName=function_name
        )
        assert BATCH_SIZE_RANGES["sqs"][0] == rs["BatchSize"]
        uuid = rs["UUID"]

        def _wait_for_event_source_mapping():
            return lambda_client.get_event_source_mapping(UUID=uuid)["State"] == "Enabled"

        assert poll_condition(_wait_for_event_source_mapping, timeout=30)

        with pytest.raises(ClientError) as e:
            # Update batch size with invalid value
            lambda_client.update_event_source_mapping(
                UUID=uuid,
                FunctionName=function_name,
                BatchSize=BATCH_SIZE_RANGES["sqs"][1] + 1,
            )
        e.match(INVALID_PARAMETER_VALUE_EXCEPTION)

        queue_url_2 = sqs_create_queue(QueueName=queue_name_2)
        queue_arn_2 = sqs_queue_arn(queue_url_2)

        with pytest.raises(ClientError) as e:
            # Create event source mapping with invalid batch size value
            lambda_client.create_event_source_mapping(
                EventSourceArn=queue_arn_2,
                FunctionName=function_name,
                BatchSize=BATCH_SIZE_RANGES["sqs"][1] + 1,
            )
        e.match(INVALID_PARAMETER_VALUE_EXCEPTION)

    def test_sqs_event_source_mapping(
        self,
        create_lambda_function,
        lambda_client,
        sqs_client,
        sqs_create_queue,
        sqs_queue_arn,
        logs_client,
        lambda_su_role,
    ):
        function_name = f"lambda_func-{short_uid()}"
        queue_name_1 = f"queue-{short_uid()}-1"

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=LAMBDA_RUNTIME_PYTHON36,
            role=lambda_su_role,
        )

        queue_url_1 = sqs_create_queue(QueueName=queue_name_1)
        queue_arn_1 = sqs_queue_arn(queue_url_1)

        lambda_client.create_event_source_mapping(
            EventSourceArn=queue_arn_1, FunctionName=function_name, MaximumBatchingWindowInSeconds=1
        )

        sqs_client.send_message(QueueUrl=queue_url_1, MessageBody=json.dumps({"foo": "bar"}))

        def assert_lambda_log_events():
            events = get_lambda_log_events(function_name=function_name, logs_client=logs_client)
            # lambda was invoked 1 time
            assert 1 == len(events[0]["Records"])

        retry(assert_lambda_log_events, sleep_before=3, retries=30)

        rs = sqs_client.receive_message(QueueUrl=queue_url_1)
        assert rs.get("Messages") is None


class TestDynamoDBEventSourceMapping:
    def test_dynamodb_event_source_mapping(
        self,
        lambda_client,
        create_lambda_function,
        create_iam_role_with_policy,
        dynamodb_client,
        dynamodb_create_table,
        logs_client,
        check_lambda_logs,
    ):
        try:
            function_name = f"lambda_func-{short_uid()}"
            role = f"test-lambda-role-{short_uid()}"
            policy_name = f"test-lambda-policy-{short_uid()}"
            role_arn = create_iam_role_with_policy(
                RoleName=role,
                PolicyName=policy_name,
                RoleDefinition=lambda_role,
                PolicyDefinition=s3_lambda_permission,
            )

            create_lambda_function(
                handler_file=TEST_LAMBDA_PYTHON_ECHO,
                func_name=function_name,
                runtime=LAMBDA_RUNTIME_PYTHON37,
                role=role_arn,
            )

            # test destination-config for create_event_source_mapping
            table_name = f"test-table-{short_uid()}"

            partition_key = "my_partition_key"
            result = dynamodb_create_table(table_name=table_name, partition_key=partition_key)

            status = result["TableDescription"]["TableStatus"]
            # wait for status "ACTIVE"
            if status != "ACTIVE":
                retry(
                    _check_table_active,
                    retries=6,
                    sleep=1.0,
                    sleep_before=2.0,
                    dynamodb_client=dynamodb_client,
                    table_name=table_name,
                )

            # activate stream
            result = dynamodb_client.update_table(
                TableName=table_name,
                StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"},
            )
            stream_arn = result["TableDescription"]["LatestStreamArn"]

            result = lambda_client.create_event_source_mapping(
                FunctionName=function_name,
                BatchSize=1,
                StartingPosition="LATEST",
                EventSourceArn=stream_arn,
                MaximumBatchingWindowInSeconds=1,
                MaximumRetryAttempts=1,
            )
            time.sleep(2)
            event_source_mapping_uuid = result["UUID"]
            event_source_mapping_state = result["State"]
            if event_source_mapping_state != "Enabled":
                retry(
                    _check_mapping_state,
                    retries=6,
                    sleep_before=2.0,
                    sleep=1.0,
                    lambda_client=lambda_client,
                    uuid=event_source_mapping_uuid,
                )

            insert = {partition_key: {"S": "hello world"}}
            dynamodb_client.put_item(TableName=table_name, Item=insert)

            def check_logs():
                # assert that logs are present
                expected = [
                    r'.*"Records":.*',
                    r'.*"dynamodb": {(.*)}.*',
                    r'.*"eventSource": ("aws:dynamodb").*',
                    r'.*"eventName": ("INSERT").*',
                    r'.*"Keys": {("my_partition_key": {"S": "hello world"})}.*',
                ]
                check_lambda_logs(function_name, expected_lines=expected)

            retry(check_logs, retries=50, sleep=2)

        finally:
            lambda_client.delete_event_source_mapping(UUID=event_source_mapping_uuid)

    def test_disabled_dynamodb_event_source_mapping(
        self,
        create_lambda_function,
        lambda_client,
        dynamodb_resource,
        dynamodb_client,
        dynamodb_create_table,
        logs_client,
        dynamodbstreams_client,
        lambda_su_role,
    ):
        function_name = f"lambda_func-{short_uid()}"
        ddb_table = f"ddb_table-{short_uid()}"

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=LAMBDA_RUNTIME_PYTHON36,
            role=lambda_su_role,
        )

        latest_stream_arn = dynamodb_create_table(
            table_name=ddb_table, partition_key="id", stream_view_type="NEW_IMAGE"
        )["TableDescription"]["LatestStreamArn"]

        rs = lambda_client.create_event_source_mapping(
            FunctionName=function_name,
            EventSourceArn=latest_stream_arn,
            StartingPosition="TRIM_HORIZON",
            MaximumBatchingWindowInSeconds=1,
        )
        uuid = rs["UUID"]

        def wait_for_table_created():
            return (
                dynamodb_client.describe_table(TableName=ddb_table)["Table"]["TableStatus"]
                == "ACTIVE"
            )

        assert poll_condition(wait_for_table_created, timeout=30)

        def wait_for_stream_created():
            return (
                dynamodbstreams_client.describe_stream(StreamArn=latest_stream_arn)[
                    "StreamDescription"
                ]["StreamStatus"]
                == "ENABLED"
            )

        assert poll_condition(wait_for_stream_created, timeout=30)

        table = dynamodb_resource.Table(ddb_table)

        items = [
            {"id": short_uid(), "data": "data1"},
            {"id": short_uid(), "data": "data2"},
        ]

        table.put_item(Item=items[0])

        def assert_events():
            events = get_lambda_log_events(function_name, logs_client=logs_client)

            # lambda was invoked 1 time
            assert 1 == len(events[0]["Records"])

        # might take some time against AWS
        retry(assert_events, sleep=3, retries=10)

        # disable event source mapping
        lambda_client.update_event_source_mapping(UUID=uuid, Enabled=False)

        table.put_item(Item=items[1])
        events = get_lambda_log_events(function_name, logs_client=logs_client)

        # lambda no longer invoked, still have 1 event
        assert 1 == len(events[0]["Records"])

    # TODO invalid test against AWS, this behavior just is not correct
    def test_deletion_event_source_mapping_with_dynamodb(
        self, create_lambda_function, lambda_client, dynamodb_client, lambda_su_role
    ):
        function_name = f"lambda_func-{short_uid()}"
        ddb_table = f"ddb_table-{short_uid()}"

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=LAMBDA_RUNTIME_PYTHON36,
            role=lambda_su_role,
        )

        latest_stream_arn = aws_stack.create_dynamodb_table(
            table_name=ddb_table,
            partition_key="id",
            client=dynamodb_client,
            stream_view_type="NEW_IMAGE",
        )["TableDescription"]["LatestStreamArn"]

        lambda_client.create_event_source_mapping(
            FunctionName=function_name,
            EventSourceArn=latest_stream_arn,
            StartingPosition="TRIM_HORIZON",
        )

        def wait_for_table_created():
            return (
                dynamodb_client.describe_table(TableName=ddb_table)["Table"]["TableStatus"]
                == "ACTIVE"
            )

        assert poll_condition(wait_for_table_created, timeout=30)

        dynamodb_client.delete_table(TableName=ddb_table)

        result = lambda_client.list_event_source_mappings(EventSourceArn=latest_stream_arn)
        assert 1 == len(result["EventSourceMappings"])

    def test_dynamodb_event_source_mapping_with_on_failure_destination_config(
        self,
        lambda_client,
        create_lambda_function,
        sqs_client,
        sqs_queue_arn,
        sqs_create_queue,
        create_iam_role_with_policy,
        dynamodb_client,
        dynamodb_create_table,
    ):
        try:
            function_name = f"lambda_func-{short_uid()}"
            role = f"test-lambda-role-{short_uid()}"
            policy_name = f"test-lambda-policy-{short_uid()}"
            role_arn = create_iam_role_with_policy(
                RoleName=role,
                PolicyName=policy_name,
                RoleDefinition=lambda_role,
                PolicyDefinition=s3_lambda_permission,
            )

            create_lambda_function(
                handler_file=TEST_LAMBDA_PYTHON_UNHANDLED_ERROR,
                func_name=function_name,
                runtime=LAMBDA_RUNTIME_PYTHON37,
                role=role_arn,
            )

            # test destination-config for create_event_source_mapping
            table_name = f"test-table-{short_uid()}"

            partition_key = "my_partition_key"
            result = dynamodb_create_table(table_name=table_name, partition_key=partition_key)

            status = result["TableDescription"]["TableStatus"]
            # wait for status "ACTIVE"
            if status != "ACTIVE":
                retry(
                    _check_table_active,
                    retries=6,
                    sleep=1.0,
                    sleep_before=2.0,
                    dynamodb_client=dynamodb_client,
                    table_name=table_name,
                )

            # activate stream
            result = dynamodb_client.update_table(
                TableName=table_name,
                StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"},
            )
            stream_arn = result["TableDescription"]["LatestStreamArn"]
            queue_event_source_mapping = sqs_create_queue()
            queue_failure_event_source_mapping_arn = sqs_queue_arn(queue_event_source_mapping)
            destination_config = {
                "OnFailure": {"Destination": queue_failure_event_source_mapping_arn}
            }

            time.sleep(2)
            result = lambda_client.create_event_source_mapping(
                FunctionName=function_name,
                BatchSize=1,
                StartingPosition="LATEST",
                EventSourceArn=stream_arn,
                MaximumBatchingWindowInSeconds=1,
                MaximumRetryAttempts=1,
                DestinationConfig=destination_config,
            )

            event_source_mapping_uuid = result["UUID"]
            event_source_mapping_state = result["State"]
            if event_source_mapping_state != "Enabled":
                retry(
                    _check_mapping_state,
                    retries=6,
                    sleep_before=2.0,
                    sleep=1.0,
                    lambda_client=lambda_client,
                    uuid=event_source_mapping_uuid,
                )

            insert = {partition_key: {"S": "hello world"}}

            dynamodb_client.put_item(TableName=table_name, Item=insert)

            def verify_failure_received():
                res = sqs_client.receive_message(QueueUrl=queue_event_source_mapping)
                msg = res["Messages"][0]
                body = json.loads(msg["Body"])
                assert body["requestContext"]["condition"] == "RetryAttemptsExhausted"
                assert body["DDBStreamBatchInfo"]["batchSize"] == 1
                assert body["DDBStreamBatchInfo"]["streamArn"] in stream_arn

            retry(verify_failure_received, retries=5, sleep=5, sleep_before=5)

        finally:
            lambda_client.delete_event_source_mapping(UUID=event_source_mapping_uuid)


class TestLambdaHttpInvocation:
    def test_http_invocation_with_apigw_proxy(self, create_lambda_function):
        lambda_name = f"test_lambda_{short_uid()}"
        lambda_resource = "/api/v1/{proxy+}"
        lambda_path = "/api/v1/hello/world"
        lambda_request_context_path = "/" + TEST_STAGE_NAME + lambda_path
        lambda_request_context_resource_path = lambda_resource

        # create lambda function
        create_lambda_function(
            func_name=lambda_name,
            handler_file=TEST_LAMBDA_PYTHON,
            libs=TEST_LAMBDA_LIBS,
        )

        # create API Gateway and connect it to the Lambda proxy backend
        lambda_uri = aws_stack.lambda_function_arn(lambda_name)
        target_uri = f"arn:aws:apigateway:{aws_stack.get_region()}:lambda:path/2015-03-31/functions/{lambda_uri}/invocations"

        result = testutil.connect_api_gateway_to_http_with_lambda_proxy(
            "test_gateway2",
            target_uri,
            path=lambda_resource,
            stage_name=TEST_STAGE_NAME,
        )

        api_id = result["id"]
        url = path_based_url(api_id=api_id, stage_name=TEST_STAGE_NAME, path=lambda_path)
        result = safe_requests.post(
            url, data=b"{}", headers={"User-Agent": "python-requests/testing"}
        )
        content = json.loads(result.content)

        assert lambda_path == content["path"]
        assert lambda_resource == content["resource"]
        assert lambda_request_context_path == content["requestContext"]["path"]
        assert lambda_request_context_resource_path == content["requestContext"]["resourcePath"]


class TestKinesisSource:
    def test_create_kinesis_event_source_mapping(
        self,
        create_lambda_function,
        lambda_client,
        kinesis_client,
        kinesis_create_stream,
        lambda_su_role,
        wait_for_stream_ready,
        logs_client,
    ):
        function_name = f"lambda_func-{short_uid()}"
        stream_name = f"test-foobar-{short_uid()}"
        num_events_kinesis = 10

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=LAMBDA_RUNTIME_PYTHON36,
            role=lambda_su_role,
        )

        kinesis_create_stream(StreamName=stream_name, ShardCount=1)
        wait_for_stream_ready(stream_name=stream_name)
        stream_summary = kinesis_client.describe_stream_summary(StreamName=stream_name)
        assert stream_summary["StreamDescriptionSummary"]["OpenShardCount"] == 1
        stream_arn = kinesis_client.describe_stream(StreamName=stream_name)["StreamDescription"][
            "StreamARN"
        ]

        # only valid against AWS / new provider (once implemented)
        if not is_old_provider():
            with pytest.raises(ClientError) as e:
                lambda_client.create_event_source_mapping(
                    EventSourceArn=stream_arn, FunctionName=function_name
                )
            e.match(INVALID_PARAMETER_VALUE_EXCEPTION)
        else:
            lambda_client.create_event_source_mapping(
                EventSourceArn=stream_arn, FunctionName=function_name, StartingPosition="LATEST"
            )
        time.sleep(2)

        kinesis_client.put_records(
            Records=[
                {"Data": "{}", "PartitionKey": f"test_{i}"} for i in range(0, num_events_kinesis)
            ],
            StreamName=stream_name,
        )

        def get_lambda_events():
            events = get_lambda_log_events(function_name, logs_client=logs_client)
            assert events
            return events

        events = retry(get_lambda_events, retries=30)
        assert len(events[0]["Records"]) == num_events_kinesis
        assert "eventID" in events[0]["Records"][0]
        assert "eventSourceARN" in events[0]["Records"][0]
        assert "eventSource" in events[0]["Records"][0]
        assert "eventVersion" in events[0]["Records"][0]
        assert "eventName" in events[0]["Records"][0]
        assert "invokeIdentityArn" in events[0]["Records"][0]
        assert "awsRegion" in events[0]["Records"][0]
        assert "kinesis" in events[0]["Records"][0]

    @patch.object(config, "SYNCHRONOUS_KINESIS_EVENTS", False)
    def test_kinesis_event_source_mapping_with_async_invocation(
        self,
        lambda_client,
        kinesis_client,
        create_lambda_function,
        kinesis_create_stream,
        wait_for_stream_ready,
        logs_client,
        lambda_su_role,
    ):

        function_name = f"lambda_func-{short_uid()}"
        stream_name = f"test-foobar-{short_uid()}"
        num_records_per_batch = 10
        num_batches = 2

        create_lambda_function(
            handler_file=TEST_LAMBDA_PARALLEL_FILE,
            func_name=function_name,
            runtime=LAMBDA_RUNTIME_PYTHON36,
            role=lambda_su_role,
        )

        kinesis_create_stream(StreamName=stream_name, ShardCount=1)
        stream_arn = kinesis_client.describe_stream(StreamName=stream_name)["StreamDescription"][
            "StreamARN"
        ]

        wait_for_stream_ready(stream_name=stream_name)

        lambda_client.create_event_source_mapping(
            EventSourceArn=stream_arn,
            FunctionName=function_name,
            StartingPosition="LATEST",
            BatchSize=num_records_per_batch,
        )

        stream_summary = kinesis_client.describe_stream_summary(StreamName=stream_name)
        assert stream_summary["StreamDescriptionSummary"]["OpenShardCount"] == 1

        for i in range(num_batches):
            start = time.perf_counter()
            kinesis_client.put_records(
                Records=[
                    {"Data": json.dumps({"record_id": j}), "PartitionKey": f"test_{i}"}
                    for j in range(0, num_records_per_batch)
                ],
                StreamName=stream_name,
            )
            assert (time.perf_counter() - start) < 1  # this should not take more than a second

        def get_lambda_invocation_events():
            events = get_lambda_log_events(
                function_name, regex_filter=r"event.*Records", logs_client=logs_client
            )
            assert len(events) == 2
            return events

        invocation_events = retry(get_lambda_invocation_events, retries=30)

        for i in range(num_batches):
            event = invocation_events[i]
            assert len(event["event"]["Records"]) == num_records_per_batch
            actual_record_ids = []
            for record in event["event"]["Records"]:
                assert "eventID" in record
                assert "eventSourceARN" in record
                assert "eventSource" in record
                assert "eventVersion" in record
                assert "eventName" in record
                assert "invokeIdentityArn" in record
                assert "awsRegion" in record
                assert "kinesis" in record
                record_data = base64.b64decode(record["kinesis"]["data"]).decode("utf-8")
                actual_record_id = json.loads(record_data)["record_id"]
                actual_record_ids.append(actual_record_id)
            actual_record_ids.sort()
            assert actual_record_ids == [i for i in range(num_records_per_batch)]

        assert (invocation_events[1]["executionStart"] - invocation_events[0]["executionStart"]) > 5

    @patch.object(config, "SYNCHRONOUS_KINESIS_EVENTS", False)
    def test_kinesis_event_source_trim_horizon(
        self,
        lambda_client,
        kinesis_client,
        create_lambda_function,
        kinesis_create_stream,
        wait_for_stream_ready,
        logs_client,
        lambda_su_role,
    ):

        function_name = f"lambda_func-{short_uid()}"
        stream_name = f"test-foobar-{short_uid()}"
        num_records_per_batch = 10
        num_batches = 3

        create_lambda_function(
            handler_file=TEST_LAMBDA_PARALLEL_FILE,
            func_name=function_name,
            runtime=LAMBDA_RUNTIME_PYTHON36,
            role=lambda_su_role,
        )

        kinesis_create_stream(StreamName=stream_name, ShardCount=1)
        stream_arn = kinesis_client.describe_stream(StreamName=stream_name)["StreamDescription"][
            "StreamARN"
        ]
        wait_for_stream_ready(stream_name=stream_name)
        stream_summary = kinesis_client.describe_stream_summary(StreamName=stream_name)
        assert stream_summary["StreamDescriptionSummary"]["OpenShardCount"] == 1

        # insert some records before event source mapping created
        for i in range(num_batches - 1):
            kinesis_client.put_records(
                Records=[
                    {"Data": json.dumps({"record_id": j}), "PartitionKey": f"test_{i}"}
                    for j in range(0, num_records_per_batch)
                ],
                StreamName=stream_name,
            )

        lambda_client.create_event_source_mapping(
            EventSourceArn=stream_arn,
            FunctionName=function_name,
            StartingPosition="TRIM_HORIZON",
            BatchSize=num_records_per_batch,
        )

        # insert some more records
        kinesis_client.put_records(
            Records=[
                {"Data": json.dumps({"record_id": i}), "PartitionKey": f"test_{num_batches}"}
                for i in range(0, num_records_per_batch)
            ],
            StreamName=stream_name,
        )

        def get_lambda_invocation_events():
            events = get_lambda_log_events(
                function_name, regex_filter=r"event.*Records", logs_client=logs_client
            )
            assert len(events) == num_batches
            return events

        invocation_events = retry(get_lambda_invocation_events, retries=30)

        for i in range(num_batches):
            event = invocation_events[i]
            assert len(event["event"]["Records"]) == num_records_per_batch
            actual_record_ids = []
            for record in event["event"]["Records"]:
                assert "eventID" in record
                assert "eventSourceARN" in record
                assert "eventSource" in record
                assert "eventVersion" in record
                assert "eventName" in record
                assert "invokeIdentityArn" in record
                assert "awsRegion" in record
                assert "kinesis" in record
                record_data = base64.b64decode(record["kinesis"]["data"]).decode("utf-8")
                actual_record_id = json.loads(record_data)["record_id"]
                actual_record_ids.append(actual_record_id)

            actual_record_ids.sort()
            assert actual_record_ids == [i for i in range(num_records_per_batch)]

    def test_disabled_kinesis_event_source_mapping(
        self,
        lambda_client,
        kinesis_client,
        create_lambda_function,
        kinesis_create_stream,
        wait_for_stream_ready,
        logs_client,
        lambda_su_role,
    ):
        function_name = f"lambda_func-{short_uid()}"
        stream_name = f"test-foobar-{short_uid()}"
        num_records_per_batch = 10

        create_lambda_function(
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            func_name=function_name,
            runtime=LAMBDA_RUNTIME_PYTHON36,
            role=lambda_su_role,
        )

        kinesis_create_stream(StreamName=stream_name, ShardCount=1)
        stream_arn = kinesis_client.describe_stream(StreamName=stream_name)["StreamDescription"][
            "StreamARN"
        ]
        wait_for_stream_ready(stream_name=stream_name)

        event_source_uuid = lambda_client.create_event_source_mapping(
            EventSourceArn=stream_arn,
            FunctionName=function_name,
            StartingPosition="LATEST",
            BatchSize=num_records_per_batch,
        )["UUID"]
        time.sleep(2)  # wait for mapping to become active

        kinesis_client.put_records(
            Records=[
                {"Data": json.dumps({"record_id": i}), "PartitionKey": f"test"}
                for i in range(0, num_records_per_batch)
            ],
            StreamName=stream_name,
        )

        def get_lambda_invocation_events():
            events = get_lambda_log_events(function_name, logs_client=logs_client)
            assert len(events) == 1
            return events

        retry(get_lambda_invocation_events, retries=30)

        lambda_client.update_event_source_mapping(UUID=event_source_uuid, Enabled=False)
        time.sleep(2)
        kinesis_client.put_records(
            Records=[
                {"Data": json.dumps({"record_id": i}), "PartitionKey": f"test"}
                for i in range(0, num_records_per_batch)
            ],
            StreamName=stream_name,
        )
        time.sleep(7)  # wait for records to pass through stream
        get_lambda_invocation_events()  # should still only get the first batch from before mapping was disabled

    def test_kinesis_event_source_mapping_with_on_failure_destination_config(
        self,
        lambda_client,
        create_lambda_function,
        sqs_client,
        sqs_queue_arn,
        sqs_create_queue,
        create_iam_role_with_policy,
        kinesis_client,
        wait_for_stream_ready,
    ):
        try:
            function_name = f"lambda_func-{short_uid()}"
            role = f"test-lambda-role-{short_uid()}"
            policy_name = f"test-lambda-policy-{short_uid()}"
            role_arn = create_iam_role_with_policy(
                RoleName=role,
                PolicyName=policy_name,
                RoleDefinition=lambda_role,
                PolicyDefinition=s3_lambda_permission,
            )

            response = create_lambda_function(
                handler_file=TEST_LAMBDA_PYTHON,
                func_name=function_name,
                runtime=LAMBDA_RUNTIME_PYTHON37,
                role=role_arn,
            )

            response["CreateFunctionResponse"]["FunctionArn"]

            # test destination-config for create_event_source_mapping
            kinesis_name = f"test-kinesis-{short_uid()}"
            kinesis_client.create_stream(StreamName=kinesis_name, ShardCount=1)
            result = kinesis_client.describe_stream(StreamName=kinesis_name)["StreamDescription"]
            kinesis_arn = result["StreamARN"]
            wait_for_stream_ready(stream_name=kinesis_name)

            queue_event_source_mapping = sqs_create_queue()
            queue_failure_event_source_mapping_arn = sqs_queue_arn(queue_event_source_mapping)
            destination_config = {
                "OnFailure": {"Destination": queue_failure_event_source_mapping_arn}
            }

            result = lambda_client.create_event_source_mapping(
                FunctionName=function_name,
                BatchSize=1,
                StartingPosition="LATEST",
                EventSourceArn=kinesis_arn,
                MaximumBatchingWindowInSeconds=1,
                MaximumRetryAttempts=1,
                DestinationConfig=destination_config,
            )
            time.sleep(2)
            event_source_mapping_uuid = result["UUID"]
            event_source_mapping_state = result["State"]
            # TODO I'm not positive this actually blocks until the mapping is available
            if event_source_mapping_state != "Enabled":
                retry(
                    _check_mapping_state,
                    retries=6,
                    sleep_before=2.0,
                    sleep=1.0,
                    lambda_client=lambda_client,
                    uuid=event_source_mapping_uuid,
                )

            message = {
                "input": "hello",
                "value": "world",
                lambda_integration.MSG_BODY_RAISE_ERROR_FLAG: 1,
            }
            kinesis_client.put_record(
                StreamName=kinesis_name, Data=to_bytes(json.dumps(message)), PartitionKey="custom"
            )

            def verify_failure_received():
                result = sqs_client.receive_message(QueueUrl=queue_event_source_mapping)
                msg = result["Messages"][0]
                body = json.loads(msg["Body"])
                assert body["requestContext"]["condition"] == "RetryAttemptsExhausted"
                assert body["KinesisBatchInfo"]["batchSize"] == 1
                assert body["KinesisBatchInfo"]["streamArn"] == kinesis_arn

            retry(verify_failure_received, retries=50, sleep=5, sleep_before=5)

        finally:
            kinesis_client.delete_stream(StreamName=kinesis_name, EnforceConsumerDeletion=True)
            lambda_client.delete_event_source_mapping(UUID=event_source_mapping_uuid)


def _check_mapping_state(lambda_client, uuid):
    assert lambda_client.get_event_source_mapping(UUID=uuid)["State"] == "Enabled"


def _check_table_active(dynamodb_client, table_name):
    state = dynamodb_client.describe_table(TableName=table_name)["Table"]["TableStatus"]
    if state != "ACTIVE":
        raise Exception(f"TableStatus is {state}")
