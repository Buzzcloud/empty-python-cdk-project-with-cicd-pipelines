import os
import logging as log
import json
from time import sleep
import datetime
import textwrap
from zipfile import ZipFile
import shutil
import boto3
from boto3.dynamodb.conditions import Key


OUTPUT_SNS_TOPIC_ARN = os.getenv("OUTPUT_SNS_TOPIC_ARN")
JOB_TABLE_NAME = os.getenv("JOB_TABLE_NAME")
TEST_ACTION_NAME = os.getenv("TEST_ACTION_NAME")
LINT_FILE = os.getenv("LINT_FILE")
TEST_FILE = os.getenv("TEST_FILE")
COVERAGE_FILE = os.getenv("COVERAGE_FILE")

JOB_MARKER = 'AJOB'

log.getLogger().setLevel(log.INFO)
sns_client = boto3.client('sns')
codepipeline_client = boto3.client('codepipeline')
dynamodb = boto3.resource('dynamodb')
job_table = dynamodb.Table(JOB_TABLE_NAME)
s3 = boto3.resource('s3')


def handler(event, _context):
    message_string = event.get('Records')[0].get('Sns').get('Message')
    message = json.loads(message_string)
    utc = message.get('time')
    detail = message.get('detail')
    pipeline = detail.get('pipeline')
    exec_id = detail.get('execution-id')
    state = detail.get('state')
    stage = detail.get('stage') or JOB_MARKER
    action = detail.get('action') or 'None'

    log.info(f'{state}: {pipeline}/{stage}/{action} {exec_id}')

    if action == 'None':
        composite_stage = stage
    else:
        composite_stage = f'{stage}: {action}'

    # If we're starting something, just create a new record
    if state == 'STARTED':
        job_table.put_item(
            Item={
                'exec_id': exec_id,
                'stage': composite_stage,
                'action': action,
                'state': state,
                'started': utc,
            }
        )
        return

    # We're not starting, so retrieve existing record for this
    job = get_job(exec_id, composite_stage)
    job['state'] = state
    if state != 'RESUMED':
        job['ended'] = datetime.datetime.utcnow().isoformat() + "Z"
    job_table.put_item(Item=job)

    # If we just ended a job, compile and send a report
    if stage == JOB_MARKER and job.get('ended'):
        # Let parallel executions for earlier steps finish
        sleep(2)
        send_report(pipeline, exec_id, state)


def get_job(exec_id, composite_stage, retries=5):
    while retries > 0:
        ddb_response = job_table.get_item(
            ConsistentRead=True,
            Key={
                'exec_id': exec_id,
                'stage': composite_stage
            },
        )
        try:
            job = ddb_response['Item']
        except:
            retries -= 1
            sleep(2)
        else:
            return job
    raise RuntimeError('The database did not sync in time.')


def send_report(pipeline, exec_id, state):
    # Fetch all data
    data = fetch_all_data(pipeline, exec_id)
    # Git revision data
    revs = data['exec']['pipelineExecution']['artifactRevisions'][0]
    commit_id = revs['revisionId']
    commit_msg = revs['revisionSummary']
    commit_url = revs['revisionUrl']
    # The Test action
    test_actions = data['action_executions']['actionExecutionDetails']
    test_action = next((x for x in test_actions if x['actionName'] == TEST_ACTION_NAME),
                       None)
    # The artifact
    artifacts = test_action['output']['outputArtifacts'][0]
    artifact_bucket = artifacts['s3location']['bucket']
    artifact_key = artifacts['s3location']['key']
    tests = get_test_results(artifact_bucket, artifact_key)
   # Get the sorted stages
    stages = get_job_stages(exec_id)
    # Start building the output string
    source_desc = source_string(commit_id, commit_msg, commit_url)
    result = f"{state}: {source_desc}\r\n\r\n"

    result += format_stages(stages)

    result += "\r\nLint:\r\n"
    result += f"\r\n{tests[LINT_FILE]}\r\n"

    result += "\r\nTests:\r\n"
    result += f"\r\n{tests[TEST_FILE]}\r\n"

    result += "\r\nCoverage:\r\n"
    result += f"\r\n{tests[COVERAGE_FILE]}\r\n"

    # Send the SNS message
    publish(result)


def format_stages(stages):
    result = ''
    # Process each stage in order, giving the first one special treatment
    for stage in stages:
        started = datetime.datetime.strptime(
            stage['started'], '%Y-%m-%dT%H:%M:%SZ')
        if stage.get('ended'):
            ended = datetime.datetime.strptime(
                stage['ended'], '%Y-%m-%dT%H:%M:%S.%fZ')
        else:
            ended = False
        if stage == stages[0]:
            result += f"The job started at {started.strftime('%H:%M:%S')} "
            result += f"and took {human_time(started, ended)}\r\n"
        elif stage['action'] == 'None':
            result += f"\r\nStage {stage['stage']} started at {started.strftime('%H:%M:%S')}\r\n"
        else:
            result += f"    {stage['action']} {stage['state'].lower()} "
            result += f"after {human_time(started, ended)}\r\n"
    return result


def get_test_results(artifact_bucket, artifact_key):
    base_dir = '/tmp'
    archive_path = f'{base_dir}/archive.zip'
    files = f'{base_dir}/extracted/'
    lint_file = f'{files}{LINT_FILE}'
    test_file = f'{files}{TEST_FILE}'
    cov_file = f'{files}{COVERAGE_FILE}'

    result = {
        LINT_FILE: 'The artifact could not be opened.',
        TEST_FILE: 'The artifact could not be opened.',
        COVERAGE_FILE: 'The artifact could not be opened.',
    }
    try:
        s3.Bucket(artifact_bucket).download_file(artifact_key, archive_path)
        with ZipFile(archive_path, 'r') as zipObj:
            zipObj.extractall(files)

        try:
            with open(lint_file) as file:
                result[LINT_FILE] = file.read().strip()
        except:
            result[LINT_FILE] = 'Could not be read.'

        try:
            with open(test_file) as file:
                result[TEST_FILE] = file.read().strip()
        except:
            result[TEST_FILE] = 'Could not be read.'

        try:
            with open(cov_file) as file:
                result[COVERAGE_FILE] = file.read().strip()
        except:
            result[COVERAGE_FILE] = 'Could not be read.'

        os.remove(archive_path)
        shutil.rmtree(files)

    except:
        pass

    return result


def get_job_stages(exec_id):
    # Get all job stages and sort them
    response = job_table.query(
        ConsistentRead=True,
        KeyConditionExpression=Key('exec_id').eq(exec_id),
    )
    stages = response['Items']
    stages.sort(key=lambda x: x.get('started'))
    return stages


def fetch_all_data(pipeline, exec_id):
    data = {}
    # To get commit revision data
    data['exec'] = codepipeline_client.get_pipeline_execution(
        pipelineName=pipeline,
        pipelineExecutionId=exec_id,
    )
    # To get the structure of the pipeline (unused at present)
    data['pipeline'] = codepipeline_client.get_pipeline(name=pipeline)
    # To get the state of the pipeline, its stages and actions
    data['state'] = codepipeline_client.get_pipeline_state(
        name=pipeline,
    )
    # To get artifact paths
    try:
        data['action_executions'] = codepipeline_client.list_action_executions(
            pipelineName=pipeline,
            filter={
                'pipelineExecutionId': exec_id,
            },
        )
    except Exception as e:
        data['action_executions_exception'] = e
        data['action_executions_exception_boto3_version'] = boto3.__version__

    return data


def publish(str):
    sns_client.publish(
        TopicArn=OUTPUT_SNS_TOPIC_ARN,
        Message=str,
    )


def pluralise(number, singular, plural):
    if number == 1:
        return f'1 {singular}'
    return f'{number} {plural}'


def human_time(started, ended):
    if not ended:
        return "an unspecified amount of time"
    seconds = (ended - started).total_seconds()
    m, s = divmod(seconds, 60)
    m = int(m)
    s = int(s)
    result = ''
    if m != 0:
        result += pluralise(m, 'minute ', 'minutes ')
        if s != 0:
            result += pluralise(s, 'second', 'seconds')
    else:
        result += pluralise(s, 'second', 'seconds')
    return result.rstrip()


def source_string(commit_id, commit_summary, commit_url):
    return f'[{commit_id[:8]}] {textwrap.shorten(commit_summary, width=50)}\r\n{commit_url}'
