import logging
import threading
import traceback
from datetime import datetime, timedelta, timezone

from localstack.utils.aws import aws_stack

# TODO used for anomaly detection models:
# LessThanLowerOrGreaterThanUpperThreshold
# LessThanLowerThreshold
# GreaterThanUpperThreshold
from localstack.utils.scheduler import Scheduler

LOG = logging.getLogger(__name__)

COMPARISON_OPS = {
    "GreaterThanOrEqualToThreshold": (lambda value, threshold: value >= threshold),
    "GreaterThanThreshold": (lambda value, threshold: value > threshold),
    "LessThanThreshold": (lambda value, threshold: value < threshold),
    "LessThanOrEqualToThreshold": (lambda value, threshold: value <= threshold),
}

STATE_ALARM = "ALARM"
STATE_OK = "OK"
STATE_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
REASON = "Alarm Evaluation"  # TODO


class AlarmScheduler:
    def __init__(self) -> None:
        super().__init__()
        self.scheduler = Scheduler()
        self.thread = threading.Thread(target=self.scheduler.run)
        self.thread.start()
        self.scheduled_alarms = {}

    def shutdown_scheduler(self):
        self.scheduler.close()
        self.thread.join(5)

    def schedule_metric_alarm(self, alarm_arn):
        """(Re-)schedules the alarm"""
        # TODO check if alarm is currently running
        alarm_details = get_metric_alarm_details_for_alarm_arn(alarm_arn)
        task = self.scheduled_alarms.get(alarm_arn)
        if task:
            task.cancel()

        period = alarm_details["Period"]
        evaluation_periods = alarm_details["EvaluationPeriods"]
        schedule_period = evaluation_periods * period

        def on_error(e):
            LOG.error(f"Error executing scheduled alarm: {e}")
            LOG.error(traceback.format_exc())

        task = self.scheduler.schedule(
            func=calculate_alarm_state, period=schedule_period, args=[alarm_arn], on_error=on_error
        )

        self.scheduled_alarms[alarm_arn] = task


def get_metric_alarm_details_for_alarm_arn(alarm_arn):
    alarm_name = aws_stack.extract_resource_from_arn(alarm_arn).split(":", 1)[1]
    client = get_cloudwatch_client_for_region_of_alarm(alarm_arn)
    return client.describe_alarms(AlarmNames=[alarm_name])["MetricAlarms"][0]


def get_cloudwatch_client_for_region_of_alarm(alarm_arn):
    region = aws_stack.extract_region_from_arn(alarm_arn)
    return aws_stack.connect_to_service("cloudwatch", region_name=region)


def generate_metric_query(alarm_details):
    return {
        "Id": alarm_details["AlarmName"],
        "MetricStat": {
            "Metric": {
                "Namespace": alarm_details["Namespace"],
                "MetricName": alarm_details["MetricName"],
                "Dimensions": alarm_details["Dimensions"],
            },
            "Period": alarm_details["Period"],
            "Stat": alarm_details["Statistic"],
        },
        # TODO other fields might be required
    }


def is_threshold_exceeded(metric_values, alarm_details):
    threshold = alarm_details["Threshold"]
    comparison_operator = alarm_details["ComparisonOperator"]
    treat_missing_data = alarm_details["TreatMissingData"]
    datapoints_to_alarm = alarm_details.get("DatapointsToAlarm", 1)
    evaluated_datapoints = []
    for value in metric_values:
        if not value:
            if treat_missing_data == "breaching":
                evaluated_datapoints.append(True)
            elif treat_missing_data == "notBreaching":
                evaluated_datapoints.append(False)
            # else we can ignore the data TODO should actually not happen
        else:
            evaluated_datapoints.append(COMPARISON_OPS.get(comparison_operator)(value, threshold))

    sum_breaching = evaluated_datapoints.count(True)
    if sum_breaching >= datapoints_to_alarm:
        return True
    return False


def is_triggering_premature_alarm(metric_values, datapoints, alarm_details):
    treat_missing_data = alarm_details["TreatMissingData"]

    if (
        datapoints == 1
        and metric_values[-1] is None
        and treat_missing_data in ("missing", "ignore")
    ):
        comparison_operator = alarm_details["ComparisonOperator"]
        threshold = alarm_details["Threshold"]
        value = list(filter(None, metric_values))[0]
        if COMPARISON_OPS.get(comparison_operator)(value, threshold):
            return True
    return False


def calculate_alarm_state(alarm_arn):
    alarm_details = get_metric_alarm_details_for_alarm_arn(alarm_arn)
    client = get_cloudwatch_client_for_region_of_alarm(alarm_arn)

    # Whenever an alarm evaluates whether to change state, CloudWatch attempts to retrieve a higher number of data points than the number specified as Evaluation Periods.
    magic_number = 2

    # The number of periods over which data is compared to the specified threshold. If you are setting an alarm that
    # requires that a number of consecutive data points be breaching to trigger the alarm, this value specifies that number.
    # If you are setting an “M out of N” alarm, this value is the N.
    evaluation_periods = alarm_details["EvaluationPeriods"]
    period = alarm_details["Period"]
    # TODO evaluation_interval = (evaluation_periods + magic_number) * alarm_details["Period"]

    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    metric_query = generate_metric_query(alarm_details)

    metric_values = []
    collected_periods = evaluation_periods + magic_number
    for i in range(0, collected_periods):
        start_time = now - timedelta(seconds=period)
        end_time = now
        metric_data = client.get_metric_data(
            MetricDataQueries=[metric_query], StartTime=start_time, EndTime=end_time
        )["MetricDataResults"][0]
        val = metric_data["Values"]
        metric_values.append(val[0] if val else None)
        now = start_time

    alarm_name = alarm_details["AlarmName"]
    alarm_state = alarm_details["StateValue"]
    treat_missing_data = alarm_details["TreatMissingData"]

    empty_datapoints = metric_values.count(None)
    if empty_datapoints == collected_periods:
        if treat_missing_data == "missing":
            client.set_alarm_state(
                AlarmName=alarm_name, StateValue=STATE_INSUFFICIENT_DATA, StateReason=REASON
            )
            return
        elif treat_missing_data == "ignore":
            return  # TODO what is the initial state for ignore?

    datapoints = len(metric_values) - empty_datapoints
    if datapoints < evaluation_periods:
        # treat missing data points
        # special case: premature alarm state https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html#CloudWatch-alarms-avoiding-premature-transition
        # However, if the last few data points are - - X - -, the alarm goes into ALARM state even if missing data points
        # are treated as missing. This is because alarms are designed to always go into ALARM state when the oldest available
        # breaching datapoint during the Evaluation Periods number of data points is at least as old as the value of Datapoints to Alarm, and all other more recent data points are breaching or missing. In this case, the alarm goes into ALARM state even if the total number of datapoints available is lower than M (Datapoints to Alarm).

        if is_triggering_premature_alarm(metric_values, datapoints, alarm_details):
            if treat_missing_data == "missing" and alarm_state != STATE_ALARM:
                client.set_alarm_state(
                    AlarmName=alarm_name, StateValue=STATE_ALARM, StateReason=REASON
                )  # TODO add region?
            # for 'ignore' the state should be retained
            return

    collected_datapoints = [val for val in reversed(metric_values) if val]
    # TODO
    while len(collected_datapoints) < evaluation_periods and treat_missing_data in (
        "breaching",
        "notBreaching",
    ):
        # breaching/non-breaching datapoints will be evaluated
        # ignore/missing are not relevant
        collected_datapoints.append(None)

    if is_threshold_exceeded(collected_datapoints, alarm_details):
        if alarm_state != STATE_ALARM:
            client.set_alarm_state(AlarmName=alarm_name, StateValue=STATE_ALARM, StateReason=REASON)
    elif alarm_state != STATE_OK:
        client.set_alarm_state(AlarmName=alarm_name, StateValue=STATE_OK, StateReason=REASON)
