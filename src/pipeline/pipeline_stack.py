from aws_cdk import (
    aws_codebuild as codebuild,
    aws_codecommit as codecommit,
    aws_codepipeline as codepipeline,
    aws_codepipeline_actions as codepipeline_actions,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
    aws_logs as logs,
    aws_dynamodb as dynamodb,
    core,
)

LINT_FILE = 'pylint.out'
TEST_FILE = 'pytest.out'
COVERAGE_FILE = 'coverage.out'


class PipelineStack(core.Stack):

    def __init__(self, scope: core.Construct, id: str,
                 project_name, stage,
                 git_repo, git_branch,
                 service_stacks,
                 build_timeout=15,
                 test_timeout=15,
                 deploy_timeout=15,
                 sns_emails=[],
                 sns_topic=None,
                 **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        if sns_emails:
            # Create an SNS topic internal to the pipeline
            internal_sns_topic = sns.Topic(self, 'InternalTopic')
            # Also re-use or create an external one for readable messages
            self.sns_topic = sns_topic or sns.Topic(self, 'ExternalTopic')

            # Subscribe the sns_emails email addresses to the external topic
            for sns_email in sns_emails:
                self.sns_topic.add_subscription(
                    sns_subscriptions.EmailSubscription(sns_email)
                )

        # The pipeline name
        pipeline_name = f'{project_name}_{stage}'

        # The pipeline. Empty for now: we're adding stages as we go along.
        pipeline = codepipeline.Pipeline(
            self, 'Pipeline',
            pipeline_name=pipeline_name,
            restart_execution_on_update=True,
        )

        # The Source stage - getting the source from the repo
        the_repo = codecommit.Repository.from_repository_name(
            self, 'Repo', git_repo)

        source_output = codepipeline.Artifact()

        source_action = codepipeline_actions.CodeCommitSourceAction(
            action_name='CodeCommit',
            repository=the_repo,
            output=source_output,
            branch=git_branch,
            trigger=codepipeline_actions.CodeCommitTrigger.EVENTS,
        )

        pipeline.add_stage(
            stage_name='Source',
            actions=[source_action],
        )

        the_source = codebuild.Source.code_commit(
            repository=the_repo,
            clone_depth=1,
        )

        # The cache bucket
        cache_bucket = s3.Bucket(self, 'Cache')

        # The Install stage - installing CDK and requirements
        install_project = codebuild.Project(
            self, f'Install_{stage}',
            project_name=f'{pipeline_name}_install',
            timeout=core.Duration.minutes(build_timeout),
            environment={
                'build_image': codebuild.LinuxBuildImage.UBUNTU_14_04_NODEJS_10_14_1
            },
            source=the_source,
            cache=codebuild.Cache.bucket(cache_bucket),
            build_spec=codebuild.BuildSpec.from_object({
                'version': 0.2,
                'phases': {
                    'build': {
                        'commands': [
                            'ls -la',
                            'python3 -m venv .env',
                            '. .env/bin/activate',
                            'npm config -g set prefer-offline true',
                            'npm config -g set cache /root/.npm',
                            'npm config get cache',
                            'npm ci',
                            'pip install -r requirements.txt',
                            'pip wheel --wheel-dir=wheels -r requirements.txt',
                            'ls -la',
                        ]
                    },
                },
                'artifacts': {
                    'files': [
                        '**/*'
                    ],
                },
                'cache': {
                    'paths': [
                        '/root/.npm/**/*',
                        '/root/.cache/pip/**/*',
                        'wheels/**/*',
                    ],
                },
            }),
        )
        install_output = codepipeline.Artifact()
        install_action = codepipeline_actions.CodeBuildAction(
            action_name='Dependencies',
            project=install_project,
            input=source_output,
            outputs=[install_output],
        )
        pipeline.add_stage(
            stage_name='Install',
            actions=[install_action],
        )

        # The Unit Test action, before synthesis
        test_action_name = 'Test'
        test_output = codepipeline.Artifact()
        test_project = codebuild.Project(
            self, f'Test_{stage}',
            project_name=f'{pipeline_name}_test',
            timeout=core.Duration.minutes(test_timeout),
            environment={
                'build_image': codebuild.LinuxBuildImage.UBUNTU_14_04_NODEJS_10_14_1
            },
            source=the_source,
            cache=codebuild.Cache.bucket(s3.Bucket(self, 'Test')),
            build_spec=codebuild.BuildSpec.from_source_filename(
                f'buildspec.{stage}.test.yml'),
        )
        test_action = codepipeline_actions.CodeBuildAction(
            action_name=test_action_name,
            type=codepipeline_actions.CodeBuildActionType.TEST,
            project=test_project,
            input=install_output,
            outputs=[test_output]
        )

        # The Build action - producing the artifacts needed to deploy
        build_project = codebuild.Project(
            self, f'Build_{stage}',
            project_name=f'{pipeline_name}_build',
            timeout=core.Duration.minutes(build_timeout),
            environment={
                'build_image': codebuild.LinuxBuildImage.UBUNTU_14_04_NODEJS_10_14_1
            },
            source=the_source,
            build_spec=codebuild.BuildSpec.from_object({
                'version': 0.2,
                'phases': {
                    'build': {
                        'commands': [
                            'ls -la',
                            '. .env/bin/activate',
                            'npm link aws-cdk --silent',
                            'pip install -q --no-index --find-links=wheels -r requirements.txt',
                            'cdk synth -o ./dist',
                            'ls -la ./dist',
                        ]
                    },
                },
                'artifacts': {
                    'files': [
                        '**/*'
                    ],
                    'base-directory': 'dist',
                },
            })
        )
        build_output = codepipeline.Artifact()
        build_action = codepipeline_actions.CodeBuildAction(
            action_name='Build',
            project=build_project,
            input=install_output,
            outputs=[build_output]
        )

        # A stage which runs Unit Tests and the Build in parallel
        pipeline.add_stage(
            stage_name='TestAndBuild',
            actions=[test_action, build_action],
        )

        # Deploy the pipeline itself. Very meta.
        deploy_pipeline_project = codebuild.Project(
            self, 'DeployPipeline',
            project_name=f'{pipeline_name}_deploy',
            timeout=core.Duration.minutes(deploy_timeout),
            environment={
                'build_image': codebuild.LinuxBuildImage.UBUNTU_14_04_NODEJS_10_14_1
            },
            build_spec=codebuild.BuildSpec.from_object({
                'version': 0.2,
                'phases': {
                    'build': {
                        'commands': [
                            'npm link aws-cdk --silent',
                            f'cdk --app . --require-approval=never deploy {id}',
                        ]
                    },
                },
            })
        )
        deploy_pipeline_project.add_to_role_policy(
            iam.PolicyStatement(
                resources=['*'],   # This needs tightening up
                actions=['*'],     # This needs tightening up
            )
        )
        # Create the action
        deploy_pipeline_action = codepipeline_actions.CodeBuildAction(
            action_name=id,
            project=deploy_pipeline_project,
            input=build_output,
        )
        # Create the pipeline deployment stage
        pipeline.add_stage(
            stage_name='DeployPipeline',
            actions=[deploy_pipeline_action]
        )

        # The Deploy stage - using the CDK artifacts to deploy each stack
        deploy_actions = []
        for stack in service_stacks:
            # Each deployment needs its own deploy action, since the buildspec differs
            deploy_project = codebuild.Project(
                self, f'DeployWorkload_{stack}',
                project_name=f'{pipeline_name}_deploy_{stack}',
                timeout=core.Duration.minutes(deploy_timeout),
                environment={
                    'build_image': codebuild.LinuxBuildImage.UBUNTU_14_04_NODEJS_10_14_1
                },
                build_spec=codebuild.BuildSpec.from_object({
                    'version': 0.2,
                    'phases': {
                        'build': {
                            'commands': [
                                'npm link aws-cdk --silent',
                                f'cdk --app . --require-approval=never deploy {stack}',
                            ]
                        },
                    },
                })
            )
            deploy_project.add_to_role_policy(
                iam.PolicyStatement(
                    resources=['*'],   # This needs tightening up
                    actions=['*'],     # This needs tightening up
                )
            )
            # Create the action
            deploy_action = codepipeline_actions.CodeBuildAction(
                action_name=stack,
                project=deploy_project,
                input=build_output,
            )

            # Add the action to the list of deployments to be executed in parallel
            deploy_actions.append(deploy_action)

        # Create the workload deployment stage
        pipeline.add_stage(
            stage_name='DeployWorkload',
            actions=deploy_actions,
        )

        # -----------------------------------------------------------
        # The rest of this file is conditional. If the list of email
        # recipients is non-empty, a Lambda and a small DynamoDB will
        # be set up to keep track of pipeline jobs and email the
        # results of tests to the given addresses.
        # -----------------------------------------------------------

        if sns_emails:

            # -----------------------------------------------------------
            # Listen to all the state changes emitted by the pipeline
            # -----------------------------------------------------------

            event_pattern = events.EventPattern(
                source=["aws.codepipeline"],
                resources=[pipeline.pipeline_arn],
                detail_type=[
                    "CodePipeline Pipeline Execution State Change",
                    "CodePipeline Stage Execution State Change",
                    "CodePipeline Action Execution State Change",
                ]
            )

            events.Rule(
                self, 'OneRuleToBindThemAll',
                event_pattern=event_pattern,
                targets=[events_targets.SnsTopic(internal_sns_topic)]
            )

            # -----------------------------------------------------------
            # A DynamoDB table to keep track of pipeline progress
            # -----------------------------------------------------------

            job_table = dynamodb.Table(
                self, 'Jobs',
                partition_key={
                    'name': 'exec_id',
                    'type': dynamodb.AttributeType.STRING,
                },
                sort_key={
                    'name': 'stage',
                    'type': dynamodb.AttributeType.STRING,
                },
            )

            # -----------------------------------------------------------
            # The lambda processing the state change topic
            # -----------------------------------------------------------

            # Get Boto3 version 1.9.205
            region = core.Stack.of(self).region
            layer = _lambda.LayerVersion.from_layer_version_arn(
                self, 'Boto3Layer',
                f'arn:aws:lambda:{region}:113088814899:layer:Klayers-python37-boto3:9',
            )

            pipeline_observer = _lambda.Function(
                self, 'PipelineObserver',
                runtime=_lambda.Runtime.PYTHON_3_7,
                code=_lambda.Code.asset('src/lambdas'),
                handler='pipeline_observer.handler',
                timeout=core.Duration.seconds(60),
                log_retention=logs.RetentionDays.THREE_MONTHS,
                environment={
                    'OUTPUT_SNS_TOPIC_ARN': self.sns_topic.topic_arn,
                    'JOB_TABLE_NAME': job_table.table_name,
                    'TEST_ACTION_NAME': test_action_name,
                    'LINT_FILE': LINT_FILE,
                    'TEST_FILE': TEST_FILE,
                    'COVERAGE_FILE': COVERAGE_FILE,

                },
                tracing=_lambda.Tracing.ACTIVE,
                layers=[layer],
            )

            job_table.grant_read_write_data(pipeline_observer)
            self.sns_topic.grant_publish(pipeline_observer)
            pipeline.artifact_bucket.grant_read(pipeline_observer)

            pipeline_observer.add_to_role_policy(
                iam.PolicyStatement(
                    resources=[pipeline.pipeline_arn],
                    actions=[
                        'codepipeline:GetPipeline',
                        'codepipeline:GetPipelineExecution',
                        'codepipeline:GetPipelineState',
                        'codepipeline:ListActionExecutions',
                    ],
                )
            )

            internal_sns_topic.add_subscription(
                sns_subscriptions.LambdaSubscription(pipeline_observer)
            )
