const https = require("https");

const PENDO_TRACK_URL = "data.pendo.io";
const PENDO_INTEGRATION_KEY = "8a5b68f1-ecb4-4a57-a657-c76b1207b5cf";

function pendoTrack(eventName, visitorId, accountId, properties) {
  return new Promise((resolve) => {
    try {
      const payload = JSON.stringify({
        type: "track",
        event: eventName,
        visitorId: visitorId || "system",
        accountId: accountId || "system",
        timestamp: Date.now(),
        properties: properties || {},
      });
      const options = {
        hostname: PENDO_TRACK_URL,
        path: "/data/track",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-pendo-integration-key": PENDO_INTEGRATION_KEY,
        },
        timeout: 5000,
      };
      const req = https.request(options, (res) => {
        res.resume();
        resolve();
      });
      req.on("error", (err) => {
        console.log(`Pendo track event '${eventName}' failed: ${err.message}`);
        resolve();
      });
      req.write(payload);
      req.end();
    } catch (err) {
      console.log(`Pendo track event '${eventName}' failed: ${err.message}`);
      resolve();
    }
  });
}

exports.handler = async function(event) {
  var method = event.httpMethod;
  var path = event.path;
  if (path === "/") {
    var response = {
      isBase64Encoded: false,
      statusCode: 200,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: "Hello Shahan"
       })
    };

    await pendoTrack("lambda_api_request_handled", "system", "system", {
      http_method: method,
      request_path: path,
      status_code: 200,
      response_type: "greeting",
    });

    return response;
  }
  if (path === '/getEvent') {
    var response = {
      isBase64Encoded: false,
      statusCode: 200,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        eventMethod: method,
        eventPath: path,
        eventHeaders: event.headers | "Cant get headers",
        eventResource: event.resource | "Cant get resource"
      })
    };

    await pendoTrack("lambda_api_request_handled", "system", "system", {
      http_method: method,
      request_path: path,
      status_code: 200,
      response_type: "event_details",
    });

    return response;
  }
};