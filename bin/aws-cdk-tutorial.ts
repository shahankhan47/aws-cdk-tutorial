#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { AwsCdkTutorialStack } from '../lib/aws-cdk-tutorial-stack';

const app = new cdk.App();
new AwsCdkTutorialStack(app, 'AwsCdkTutorialStack', {
  stackName: 'ss-shahan-local-stack',
  description: 'Local Stack for CDK Tutorial',
  env: {
    account: '301252497296',
    region: 'us-east-1'
  }
});