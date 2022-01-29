#!/usr/bin/env python3
from aws_cdk import (
    core,
)

from app_stack import YourAppStack
from pipeline.pipeline_stack import PipelineStack

app = core.App()

env = {'account': '9999999999999', 'region': 'xx-xxxx-9'}

tags = {
    # Tailor these as you wish
    'Workload': 'your-app',
    'Team': 'YourTeam',
    'Name': 'Your Name',
    'Email': 'your.name@example.com',
    'Phone': '9999999999999',
}

YourAppStack(app, "app-dev", env=env, tags=tags)
YourAppStack(app, "app-staging", env=env, tags=tags)
YourAppStack(app, "app-prod", env=env, tags=tags)

dev_stack = PipelineStack(
    app, 'pipeline-dev', env=env, tags=tags,
    project_name='YourApp', stage='dev',
    git_repo='your-gitcommit-repo', git_branch='master',
    service_stacks=['app-dev'],
    sns_emails=['your.name@example.com'],
)

PipelineStack(
    app, 'pipeline-staging', env=env, tags=tags,
    project_name='YourApp', stage='staging',
    git_repo='your-gitcommit-repo', git_branch='staging',
    service_stacks=['app-staging'],
    sns_emails=None,
)

PipelineStack(
    app, 'pipeline-prod', env=env, tags=tags,
    project_name='YourApp', stage='prod',
    git_repo='your-gitcommit-repo', git_branch='prod',
    service_stacks=['app-prod'],
    sns_emails=['some.other.name@example.com'],
)

app.synth()
