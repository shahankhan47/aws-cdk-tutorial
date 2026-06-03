#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import * as https from 'https';
import { AwsCdkTutorialStack } from '../lib/aws-cdk-tutorial-stack';

const PENDO_TRACK_URL = 'data.pendo.io';
const PENDO_INTEGRATION_KEY = '8a5b68f1-ecb4-4a57-a657-c76b1207b5cf';

function pendoTrack(eventName: string, properties: Record<string, unknown>): void {
  try {
    const payload = JSON.stringify({
      type: 'track',
      event: eventName,
      visitorId: 'system',
      accountId: 'system',
      timestamp: Date.now(),
      properties,
    });
    const options: https.RequestOptions = {
      hostname: PENDO_TRACK_URL,
      path: '/data/track',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-pendo-integration-key': PENDO_INTEGRATION_KEY,
      },
      timeout: 5000,
    };
    const req = https.request(options, (res) => { res.resume(); });
    req.on('error', (err) => {
      console.log(`Pendo track event '${eventName}' failed: ${err.message}`);
    });
    req.write(payload);
    req.end();
  } catch (err) {
    console.log(`Pendo track event '${eventName}' failed: ${err}`);
  }
}

const stackConfig = {
  stackName: 'ss-shahan-local-stack',
  description: 'Local Stack for CDK Tutorial',
  env: {
    account: '301252497296',
    region: 'us-east-1',
  },
};

const app = new cdk.App();
new AwsCdkTutorialStack(app, 'AwsCdkTutorialStack', stackConfig);

pendoTrack('infrastructure_stack_deployed', {
  stack_name: stackConfig.stackName,
  aws_account: stackConfig.env.account,
  aws_region: stackConfig.env.region,
});