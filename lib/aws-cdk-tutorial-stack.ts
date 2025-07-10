import { Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as lambda from 'aws-cdk-lib/aws-lambda';

export class AwsCdkTutorialStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);
 
    // defines an AWS Lambda resource
    const demoFn = new lambda.Function(this, 'DemoFunctionId', {
      runtime: lambda.Runtime.NODEJS_14_X,
      code: lambda.Code.fromAsset('lambda'),
      handler: 'demo.handler'
    });

    // defines an API Gateway
    const api = new apigw.RestApi(this, 'ApiGatewayId', {
      restApiName: 'my-rest-api',
      description: 'New REST API without default integrations'
    });

    const integration = new apigw.LambdaIntegration(demoFn);
    api.root.addMethod('GET', integration);
    api.root.addResource('getEvent').addMethod('GET', integration);
  }
}
